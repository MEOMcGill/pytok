"""Runtime patches for zendriver's auto-generated CDP bindings.

zendriver (like its upstream, nodriver) generates its CDP dataclasses from the
Chrome DevTools Protocol spec and lags behind Chrome's protocol changes. Chrome
149 renamed ``ClientSecurityState.privateNetworkRequestPolicy`` to
``localNetworkAccessRequestPolicy`` (and gave it a different enum). zendriver's
``from_json`` still accesses the old key unconditionally, so every
``Network.requestWillBeSentExtraInfo`` event raises ``KeyError`` while being
parsed and is silently dropped by the event listener.

Both zendriver and nodriver share this bug, so swapping libraries does not help.
We patch the affected ``from_json`` to accept either key and tolerate the new
enum values instead.
"""

import logging

logger = logging.getLogger(__name__)


def apply_cdp_patches() -> None:
    """Patch zendriver CDP bindings for Chrome 149+ compatibility. Idempotent."""
    from zendriver.cdp import network

    cls = network.ClientSecurityState
    if getattr(cls, "_pytok_patched", False):
        return

    PrivateNetworkRequestPolicy = network.PrivateNetworkRequestPolicy
    IPAddressSpace = network.IPAddressSpace

    def from_json(cls, json):
        raw = json.get("privateNetworkRequestPolicy")
        if raw is None:
            # Chrome 149+ sends localNetworkAccessRequestPolicy instead.
            raw = json.get("localNetworkAccessRequestPolicy")
        try:
            policy = PrivateNetworkRequestPolicy(raw) if raw is not None else None
        except ValueError:
            # Local-network-access uses different enum values; we don't read
            # this field, so a best-effort None keeps the event parseable.
            policy = None
        return cls(
            initiator_is_secure_context=bool(json["initiatorIsSecureContext"]),
            initiator_ip_address_space=IPAddressSpace.from_json(json["initiatorIPAddressSpace"]),
            private_network_request_policy=policy,
        )

    cls.from_json = classmethod(from_json)
    cls._pytok_patched = True
    logger.debug("Applied Chrome 149 ClientSecurityState CDP patch to zendriver")
