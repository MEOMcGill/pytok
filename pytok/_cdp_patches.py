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

import asyncio
import dataclasses
import logging
import os

from .exceptions import CDPTimeoutException

logger = logging.getLogger(__name__)

# Upper bound on how long a single CDP command may wait for its reply. Any real command
# answers in milliseconds; the slowest legitimate one is the in-page base64 video fetch,
# whose caller already caps it well below this. So this is not a performance knob — it exists
# purely so a command that will *never* be answered cannot hang its coroutine forever.
DEFAULT_CDP_TIMEOUT = 120.0


def _cdp_timeout():
    """Seconds to allow one CDP command, or None to disable the bound entirely."""
    raw = os.environ.get("PYTOK_CDP_TIMEOUT")
    if raw is None:
        return DEFAULT_CDP_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Ignoring PYTOK_CDP_TIMEOUT=%r (not a number); using %gs", raw,
            DEFAULT_CDP_TIMEOUT,
        )
        return DEFAULT_CDP_TIMEOUT
    # <= 0 is the escape hatch for debugging a command that legitimately takes forever
    return value if value > 0 else None


def _cdp_command_name(cdp_obj) -> str:
    """Name the command without touching the generator.

    Transaction.__init__ gets the real method name via ``next(cdp_obj)``, which *consumes*
    the first yield — doing that here would corrupt the command before it is sent. The
    generator's code name ("get_cookies", "evaluate") is close enough for an error message.
    """
    try:
        return cdp_obj.gi_code.co_qualname
    except AttributeError:
        return getattr(getattr(cdp_obj, "gi_code", None), "co_name", "?")

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


def _patch_connection_send_timeout() -> None:
    """Bound every CDP command so an unanswered one can't hang forever. Idempotent.

    ``Connection.send`` ends in ``return await tx`` on a bare Future. Nothing resolves that
    future except a reply from the browser, and nothing bounds the wait — so any page that
    stops servicing the DevTools protocol hangs its caller permanently, and each caller has
    to remember to impose its own timeout.

    Relying on callers is what kept biting us. ``video.bytes()`` grew a timeout, which fixed
    the byte fetch and moved the hang to the listing walk instead: on 2026-08-05 the media
    backfill lost all three pool workers one at a time over two hours to CDP calls elsewhere
    in pytok, ending with an alive-but-idle event loop, every thread-pool thread idle, and no
    error of any kind in the log. Bounding it here covers every call site at once, including
    the ones nobody has thought about yet.

    On timeout we raise ``CDPTimeoutException`` rather than letting ``asyncio.TimeoutError``
    out: the bare one stringifies to ``''`` (which has already cost us one debugging session),
    and the named one lands in ``Worker.execute_task``'s generic handler, which rebuilds the
    session in place — the right recovery for a dead browser.

    Depends on the transaction dispatch guard above. ``wait_for`` *cancels* the transaction on
    timeout, so a late reply would otherwise hit a cancelled future, raise
    ``InvalidStateError`` inside the listener and kill it — turning one bounded hang into a
    permanently dead connection. Bounding ``send`` without that guard would be worse than not
    bounding it at all, which is why ``apply_cdp_patches`` installs them in this order.
    """
    from zendriver.core.connection import Connection

    # marker on the function, not the class: Connection's CantTouchThis metaclass refuses
    # attribute assignment outright, so there is nowhere on the class to record this.
    if getattr(Connection.send, "_pytok_bounded", False):
        return

    original_send = Connection.send

    async def send(self, cdp_obj, _is_update: bool = False):
        timeout = _cdp_timeout()
        if timeout is None:
            return await original_send(self, cdp_obj, _is_update)
        try:
            return await asyncio.wait_for(
                original_send(self, cdp_obj, _is_update), timeout=timeout
            )
        except asyncio.TimeoutError:
            raise CDPTimeoutException(
                f"CDP command {_cdp_command_name(cdp_obj)} was not answered within "
                f"{timeout:g}s — the page has stopped servicing DevTools, so this session "
                f"needs rebuilding"
            ) from None

    send._pytok_bounded = True
    # Connection's metaclass (CantTouchThis) rejects every class-level assignment, to stop
    # callers creating shared mutable class state like `websocket`. Replacing a method is not
    # that, so go under it via type.__setattr__ rather than leaving the hang unfixed.
    type.__setattr__(Connection, "send", send)
    logger.debug("Bounded zendriver CDP commands at %s", _cdp_timeout())


def apply_cdp_patches() -> None:
    """Apply every zendriver runtime patch. Idempotent."""
    # order matters: the dispatch guard has to be in place before send() starts cancelling
    # transactions on timeout, or a late reply to a cancelled one kills the listener.
    _patch_transaction_dispatch()
    _patch_connection_send_timeout()
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
