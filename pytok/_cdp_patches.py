"""Runtime patches for zendriver's auto-generated CDP bindings.

zendriver (like its upstream, nodriver) generates its CDP dataclasses from the
Chrome DevTools Protocol spec, so its ``ClientSecurityState`` tracks whichever
spec revision it was generated from. Chrome 149 renamed
``ClientSecurityState.privateNetworkRequestPolicy`` to
``localNetworkAccessRequestPolicy`` and gave it a different enum, and zendriver's
generated ``from_json`` reads its key with ``json[...]`` — unconditionally. So any
mismatch between the Chrome build and the zendriver build raises ``KeyError``
while parsing ``Network.requestWillBeSentExtraInfo``, and the event is silently
dropped by the event listener.

That cuts both ways, which is why this patch is version-aware rather than pinned
to either spelling:

* older zendriver + Chrome 149+  -> generated code wants ``privateNetworkRequestPolicy``,
  Chrome sends ``localNetworkAccessRequestPolicy``
* newer zendriver + older Chrome -> exactly the reverse

zendriver 0.15.5 completed the rename (field ``local_network_access_request_policy``,
enum ``LocalNetworkAccessRequestPolicy``); earlier releases expose only the old
names. We detect what the installed zendriver actually has and patch ``from_json``
to accept *either* JSON key and tolerate enum values it doesn't recognise. Both
zendriver and nodriver share the underlying fragility, so swapping libraries does
not help.
"""

import dataclasses
import logging

logger = logging.getLogger(__name__)

# (dataclass field name, JSON key) for each spelling of the policy, newest first.
_POLICY_SPELLINGS = (
    ("local_network_access_request_policy", "localNetworkAccessRequestPolicy"),
    ("private_network_request_policy", "privateNetworkRequestPolicy"),
)


def apply_cdp_patches() -> None:
    """Make ClientSecurityState parse across Chrome/zendriver versions. Idempotent."""
    from zendriver.cdp import network

    cls = network.ClientSecurityState
    if getattr(cls, "_pytok_patched", False):
        return

    IPAddressSpace = network.IPAddressSpace

    # Whichever enum this zendriver ships. Only used to coerce the raw string; the
    # field is never read by pytok, so failing to resolve it is not fatal.
    policy_enum = getattr(network, "LocalNetworkAccessRequestPolicy", None) or getattr(
        network, "PrivateNetworkRequestPolicy", None
    )

    # The constructor kwarg has to match this zendriver's dataclass field exactly.
    field_names = {f.name for f in dataclasses.fields(cls)}
    policy_kwarg = next(
        (name for name, _ in _POLICY_SPELLINGS if name in field_names), None
    )
    if policy_kwarg is None:
        # A zendriver whose ClientSecurityState has neither spelling — the shape has
        # changed beyond what this patch understands. Leave it alone rather than
        # installing a from_json that would raise TypeError on every event.
        logger.warning(
            "Skipping ClientSecurityState CDP patch: no known policy field in %s",
            sorted(field_names),
        )
        return

    def from_json(cls, json):
        raw = None
        for _, json_key in _POLICY_SPELLINGS:
            if json_key in json:
                raw = json[json_key]
                break

        policy = None
        if raw is not None and policy_enum is not None:
            try:
                policy = policy_enum(raw)
            except ValueError:
                # The old and new enums do not share values. pytok never reads this
                # field, so a best-effort None keeps the event parseable instead of
                # letting the listener drop it.
                logger.debug("Unrecognised network access policy value %r", raw)

        return cls(
            initiator_is_secure_context=bool(json["initiatorIsSecureContext"]),
            initiator_ip_address_space=IPAddressSpace.from_json(
                json["initiatorIPAddressSpace"]
            ),
            **{policy_kwarg: policy},
        )

    cls.from_json = classmethod(from_json)
    cls._pytok_patched = True
    logger.debug(
        "Applied ClientSecurityState CDP patch to zendriver (field=%s)", policy_kwarg
    )
