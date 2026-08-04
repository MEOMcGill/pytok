"""Runtime patches for zendriver.

Two unrelated fragilities, both of which cost us whole scraping sessions:
``apply_cdp_patches`` fixes ``ClientSecurityState`` parsing (below) and guards CDP
response dispatch so a late reply can't kill the connection's listener task (see
``_patch_transaction_dispatch``).

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


def _patch_transaction_dispatch() -> None:
    """Stop a late CDP response from killing the connection's listener task. Idempotent.

    ``Transaction`` is an ``asyncio.Future`` and ``Transaction.__call__`` resolves it with
    ``set_result`` unconditionally. ``asyncio.wait_for`` *cancels* that future when it times
    out, so a reply that arrives after the caller gave up lands on an already-cancelled
    future and ``set_result`` raises ``InvalidStateError``. That propagates out of the bare
    ``tx(**message)`` in ``Listener.listener_loop`` — the command-response branch has no
    exception handling, unlike the event branch below it — so the listener task dies. After
    that no CDP reply is ever delivered again and every later ``page.send()`` waits forever
    on a future nothing will resolve: the session is wedged permanently, and because
    ``send()`` has no timeout of its own, only the caller's own timeout bounds it.

    That is how one timed-out ``Network.getCookies`` took out a whole scraping session on
    2026-08-04 (16 ``InvalidStateError``s; both pool workers stuck holding their accounts,
    the run silent for ~50 minutes). Tightening a caller-side timeout makes it *more* likely,
    since abandoning a transaction is what creates the race.

    Guarding ``__call__`` rather than ``listener_loop`` keeps this small and version-robust:
    we delegate to the original for everything we don't need to change, instead of
    reimplementing the loop body. Neither zendriver 0.15.5 (the newest release) nor upstream
    ``main`` guards this, so there is no version to upgrade to.
    """
    from zendriver.core.connection import Transaction

    if getattr(Transaction, "_pytok_dispatch_guarded", False):
        return

    original_call = Transaction.__call__

    def __call__(self, **response):  # noqa: N807 - patching a dunder on someone else's class
        if self.done():
            # Caller already gave up (wait_for cancelled us), or we were resolved once
            # already. Delivering now is a no-op, not a reason to bring the listener down.
            logger.debug(
                "Dropping CDP response for settled transaction %s",
                getattr(self, "method", "?"),
            )
            return None
        try:
            return original_call(self, **response)
        except Exception as ex:
            # Anything else the original raises (e.g. ProtocolException for a response it
            # cannot parse) also reaches the listener as a fatal error. Fail just this
            # transaction instead: whoever is awaiting it gets a real exception rather than
            # hanging, and the connection stays usable for every other transaction.
            logger.warning(
                "CDP response dispatch failed for %s: %r",
                getattr(self, "method", "?"),
                ex,
            )
            if not self.done():
                self.set_exception(ex)
            return None

    Transaction.__call__ = __call__
    Transaction._pytok_dispatch_guarded = True
    logger.debug("Applied CDP transaction dispatch guard to zendriver")


def apply_cdp_patches() -> None:
    """Apply every zendriver runtime patch. Idempotent."""
    _patch_transaction_dispatch()
    _apply_client_security_state_patch()


def _apply_client_security_state_patch() -> None:
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
