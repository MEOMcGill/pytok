"""Scrape TikTok mobile web by driving Chrome on a physical Android phone over adb + CDP.

A third data route, alongside the two in `tiktok.py` (signed API requests, and camoufox
driving a desktop Firefox). Instead of a browser on this machine it drives **Chrome on a
real handset**:

    wake the phone -> start Chrome -> `adb forward` its DevTools socket -> open a tab at
    the profile URL -> capture the `/api/...` responses off the wire with CDP -> parse the
    same payloads the desktop route parses.

Why bother: the handset presents a genuine mobile fingerprint (real touch, real DPR, real
mobile Chrome build) that no desktop stealth patch reproduces. In testing, an anonymous
phone session returned a full `item_list` for a profile with no captcha and no login,
where anonymous desktop sessions mostly come back empty.

What this does NOT buy you
--------------------------
**IP diversity.** Every phone on one WiFi shares a single egress IP, and TikTok's burst
limits are largely IP-level, so rotating handsets spreads the per-*session* load but not
the per-*IP* load. Phones with their own SIMs or proxies would; phones on a shared AP do
not. Do not size a crawl on the assumption that N phones give N times the headroom.

**Deep pagination.** Mobile web hands over the first `item_list` page (~35 videos) and
then stops: there is no infinite scroll to drive. `user_videos()` documents its own
ceiling and says when it hit it.

Forging the next page from inside the page does not work, and it is worth recording why,
because the obvious diagnosis is wrong. It is *not* a signing problem:

- TikTok's `webmssdk.js` patches `fetch` and `XMLHttpRequest` (none of them are native),
  and `/api/post/item_list/` is in its `_mssdk._enablePathList`. A `fetch()` issued from
  page context with `X-Bogus`/`X-Gnarly` omitted comes back out on the wire *with both
  freshly added* -- verified against `Network.requestWillBeSentExtraInfo`.
- Diffing that outgoing request against the app's own: the header sets are identical, no
  header only one of them sends.
- `msToken` is not a rotating single-use nonce -- the one in the captured URL and the one
  in `document.cookie` are the same string.

And yet, seconds apart on one page load, the app's own request returns ~82 KB while ours
returns a 200 with an empty body -- including when replayed completely unchanged. TikTok
is separating app-initiated from injected requests on something that is not in the URL or
the headers, so re-signing harder will not fix it.

Driving the *desktop* bundle from the handset (UA + viewport override) does make the app
issue its own paginating requests, but anonymously every one of them comes back empty --
the same thing desktop sessions do without an account. So depth needs a logged-in
session, not better forgery; the account pool in `pytok.accounts` is the lead worth
following, not this module.

So: use this for breadth (first page for many profiles, from a fingerprint TikTok treats
differently), not for depth on one profile.

Prerequisites
-------------
- `adb` on the host the phones hang off, and USB debugging authorized per handset
  (a phone in adb state "unauthorized" still needs its on-screen prompt accepted).
- Chrome installed on the phone. It need not be logged in.
- The phone must be **awake**: Chrome does not open its DevTools socket while the screen
  is off, so a sleeping handset fails with "DevTools socket never appeared". `keep_awake`
  (default) handles this.

Usage::

    from pytok.phone import PhoneTikTok, list_serials

    serials = await list_serials(ssh_host="meo-laptop", adb=r"C:\\platform-tools\\adb.exe")
    async with PhoneTikTok(serials[0], ssh_host="meo-laptop",
                           adb=r"C:\\platform-tools\\adb.exe") as phone:
        info = await phone.user_info("therock")
        videos = await phone.user_videos("therock")
        feed = await phone.for_you(count=50)

`for_you()` reads the home feed by swiping through it. Mobile web stops moving the feed
after its first batch, so it returns about five videos logged out and eight or nine
logged in (a phone bound to a pool account, see `from_pool`), and no more.

The transport here (adb wrapper, CDP tunnel, the three gotchas below) follows
`ai_scrapers.phone_farm`, which drives the same handsets for Google AI Mode. It is
reimplemented rather than imported because that one is synchronous and pytok is
async throughout, and because this needs CDP *network capture*, which it does not do.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import websockets

from .exceptions import (
    ApiFailedException,
    CaptchaException,
    NotFoundException,
    TikTokException,
)
from .utils import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

CHROME_PKG = "com.android.chrome"
_CHROME_MAIN = "com.google.android.apps.chrome.Main"
DEFAULT_CDP_PORT = 9222
PROFILE_URL = "https://www.tiktok.com/@{username}"

# The endpoint worth keeping when it goes past on the wire. Substring match on the URL,
# because it carries a long signed query string. Profile info does not need capturing:
# the mobile page ships it in its rehydration payload and never calls /api/user/detail/.
_ITEM_LIST_EP = "/api/post/item_list/"
_RECOMMEND_EP = "/api/recommend/item_list/"
FOR_YOU_URL = "https://www.tiktok.com/foryou"
LOGIN_URL = "https://www.tiktok.com/login/phone-or-email/email"

GMAIL_PKG = "com.google.android.gm"
# How long to wait for TikTok's emailed login code to reach the phone's Gmail inbox.
_EMAIL_CODE_TIMEOUT = 180.0
# TikTok's code email as Gmail's inbox list describes it to accessibility services,
# e.g. "Unread, TikTok, 2 messages, 123456 is your 6-digit code, ..., 3:19 p.m.". The
# whole description identifies the row: a resent code can repeat the previous one.
# The row's text sits in `text` or `content-desc` depending on the Gmail build.
_TIKTOK_CODE_ROW_RE = re.compile(
    r'="([^"]*TikTok, (?:\d+ messages, )?(\d{6}) is your 6-digit code[^"]*)"')
# The "verify it's really you" step: a button per method, of which we use the email one.
_CHOOSE_EMAIL_METHOD_JS = r"""
(() => {
  const b = [...document.querySelectorAll('button')]
    .find(b => b.offsetParent && /^Email/.test(b.innerText.trim()));
  if (!b) return false;
  b.click();
  return true;
})()
"""
_CODE_INPUT = 'input[type="tel"]'
_SUBMIT_CODE_JS = r"""
(() => {
  const b = [...document.querySelectorAll('button')].find(b => b.offsetParent && !b.disabled
    && /^(next|verify|submit|confirm|continue)$/i.test(b.innerText.trim()));
  if (!b) return null;
  b.click();
  return b.innerText.trim();
})()
"""

# Cookies TikTok sets once a session is logged in.
_SESSION_COOKIES = {"sessionid", "sessionid_ss", "sid_tt"}
_LOGIN_FIELDS = ('input[name="username"]', 'input[type="password"]')
# What the login page shows instead of logging in: a captcha, or an error under the form.
_LOGIN_OBSTACLE_JS = r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  const captcha = ['Drag the slider', 'Verify to continue', 'Rotate the shapes',
                   'Select 2 objects', 'Click on the shapes'].find(t => text.includes(t));
  if (captcha || document.querySelector('[id*="captcha" i] canvas, [class*="captcha" i] img'))
    return 'captcha: ' + (captcha || 'puzzle shown');
  const errors = ['incorrect', "doesn't exist", 'too many attempts', 'something went wrong',
                  'try again later', 'maximum number of attempts'];
  const lower = text.toLowerCase();
  const error = errors.find(e => lower.includes(e));
  return error ? 'error: ' + text.slice(Math.max(0, lower.indexOf(error) - 60), lower.indexOf(error) + 60).trim() : null;
})()
"""

# Mobile web's For You feed is a swiper fetching small batches a few videos ahead, so
# several swipes in a row can go by without a request; this many with nothing new ends
# the walk.
_FOR_YOU_STALL_SWIPES = 12
# A touch the renderer never acknowledges means the tab is not on screen: the phone
# went to sleep, or something else brought its own tab or app to the front.
# Swipes in a row that leave the same video in front before the walk gives up. Mobile
# web stops moving its feed after the first batch, logged in or not.
_FOR_YOU_STUCK_SWIPES = 3
_ACTIVE_SLIDE_JS = r"""
(() => {
  const a = document.querySelector('[data-e2e="video-slide-active"]');
  return a ? a.innerText.slice(0, 200) : null;
})()
"""
_TOUCH_TIMEOUT = 15.0
_TOUCH_FAILURES_ALLOWED = 3

# Dismisses what mobile web lays over the feed: the "open the app" sheet it raises after
# a swipe, and the privacy notice a newly logged-in account gets. While either is up,
# swipes go to it rather than the feed. A script click, never a touch: the app sheet's
# "Open TikTok Lite" button sits right beside "Not now" and launches the app.
_DISMISS_APP_PROMPT_JS = r"""
(() => {
  const b = document.querySelector('[data-e2e="alt-middle-cta-cancel-btn"]')
    || [...document.querySelectorAll('button')]
         .find(b => b.offsetParent && b.innerText.trim() === 'Got it');
  if (!b) return false;
  b.click();
  return true;
})()
"""


class PhoneError(TikTokException):
    """adb / CDP transport failure while driving a phone.

    A TikTokException so callers that already funnel pytok failures into one handler keep
    working, but distinct from the API/scraping exceptions: it means the *handset* could
    not be driven, not that TikTok refused us.
    """


# ── adb transport (local, or over one SSH hop) ────────────────────────────────

@dataclass
class AsyncAdb:
    """Runs adb, either locally or by prefixing one SSH hop to a remote host."""

    adb: str = "adb"
    ssh_host: Optional[str] = None
    timeout: int = 60

    async def _run(self, cmd: str) -> tuple[int, str, str]:
        if self.ssh_host:
            proc = await asyncio.create_subprocess_exec(
                "ssh", self.ssh_host, cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        else:
            # The platform's own shell: on a Windows farm host reached locally this is
            # cmd.exe, and every command built here is plain enough to survive both.
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise PhoneError(f"adb command timed out after {self.timeout}s: {cmd}")
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def shell(self, serial: str, script: str) -> str:
        """Run `adb -s <serial> shell "<script>"`.

        `script` runs in the phone's own sh, so `;`-chaining works. Wrap URLs in single
        quotes inside it so `&` survives: cmd.exe on a Windows ssh host keeps `&` literal
        because the whole argument is double-quoted.
        """
        _, out, _ = await self._run(f'{self.adb} -s {serial} shell "{script}"')
        return out

    async def raw(self, serial: str, args: str) -> str:
        _, out, _ = await self._run(f"{self.adb} -s {serial} {args}")
        return out

    async def devices(self) -> list[tuple[str, str]]:
        """`adb devices` as (serial, state) pairs, e.g. ("R5CY23A517W", "device")."""
        _, out, _ = await self._run(f"{self.adb} devices")
        pairs = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                pairs.append((parts[0], parts[1]))
        return pairs


async def list_serials(*, ssh_host: Optional[str] = None, adb: Optional[str] = None,
                       timeout: int = 60) -> list[str]:
    """Serials of the phones that are drivable right now.

    Only handsets in adb state "device" are returned. An "unauthorized" one needs its
    on-screen USB-debugging prompt accepted first, so it stays invisible to a run rather
    than failing one midway.
    """
    transport = AsyncAdb(adb=adb or os.getenv("PYTOK_ADB", "adb"),
                         ssh_host=ssh_host or os.getenv("PYTOK_PHONE_SSH") or None,
                         timeout=timeout)
    pairs = await transport.devices()
    ready = [s for s, state in pairs if state == "device"]
    if not ready and pairs:
        logger.warning("no drivable phones; adb reports: "
                       + ", ".join(f"{s}={st}" for s, st in pairs))
    return ready


# ── CDP tunnel: adb forward + (optional) SSH -L, kept alive together ──────────

class CdpTunnel:
    """Expose the phone's Chrome DevTools endpoint at http://127.0.0.1:<port>.

    Three non-obvious things this has to get right, all learned the hard way:

    1. Over SSH the forward is established *inside* the same long-lived process that holds
       the `-L` tunnel open, so one process owns both ends. The forward itself belongs to
       the adb server and outlives that process, so `__aexit__` removes it explicitly.
    2. The tunnel's remote target must be `127.0.0.1`, not `localhost`: the latter can
       resolve to `::1` while adb binds IPv4, and the connection then hangs.
    3. `localabstract:chrome_devtools_remote` only exists *while Chrome runs*, so the
       forward succeeds against a dead Chrome and every connection through it is refused.
       `PhoneTikTok._ensure_chrome` starts Chrome before the tunnel is built.
    """

    def __init__(self, adb: AsyncAdb, serial: str, port: int = DEFAULT_CDP_PORT):
        self._adb = adb
        self._serial = serial
        self._port = port
        self._proc: Optional[asyncio.subprocess.Process] = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def __aenter__(self) -> "CdpTunnel":
        fwd = (f"{self._adb.adb} -s {self._serial} forward tcp:{self._port} "
               f"localabstract:chrome_devtools_remote")
        if self._adb.ssh_host:
            # Windows keepalive that blocks on stdin, so it exits when the ssh channel
            # closes; a timed one (ping) outlives a killed ssh client on the host. The
            # forward and the -L tunnel must share one process (gotcha 1 above).
            keepalive = "findstr pytok-keepalive-never-matches"
            self._proc = await asyncio.create_subprocess_exec(
                # Its own connection, not a shared ControlMaster: tearing down a master
                # takes every tunnel multiplexed onto it with it.
                "ssh", "-o", "ControlPath=none", "-L", f"{self._port}:127.0.0.1:{self._port}",
                self._adb.ssh_host, f"{fwd} && {keepalive}",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        else:
            rc, out, err = await self._adb._run(fwd)
            if rc != 0:
                raise PhoneError(f"`adb forward` failed for {self._serial} (rc {rc}): "
                                 f"{(err or out).strip()[:200]}")
        await self._wait_ready()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        # Always remove the forward, including over SSH. It is registered with the *adb
        # server*, a daemon that outlives the client that created it, so killing the ssh
        # session drops the -L tunnel but leaves the forward behind. Left alone they
        # accumulate, and a stale one on a port later reused for a different handset
        # would quietly point at the wrong phone.
        await self._adb.raw(self._serial, f"forward --remove tcp:{self._port}")

    # A busy farm host can take tens of seconds to accept a fresh ssh connection.
    async def _wait_ready(self, timeout: float = 90.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last: Any = None
        while loop.time() < deadline:
            try:
                await self.get_json("/json/version")
                return
            except Exception as ex:  # noqa: BLE001 — retry any transport hiccup
                last = ex
                await asyncio.sleep(0.7)
        raise PhoneError(f"CDP endpoint never came up on {self.base}: {last}")

    async def get_json(self, path: str) -> Any:
        """GET a DevTools HTTP endpoint.

        Uses httpx rather than a hand-rolled request because Android Chrome's DevTools
        server keeps the connection open regardless of `Connection: close`, so reading to
        EOF just hangs until the timeout.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(self.base + path)
        resp.raise_for_status()
        return resp.json()


# ── CDP session over the browser-level websocket ──────────────────────────────

class CdpSession:
    """One websocket to the phone Chrome's browser endpoint, with flat target sessions.

    Attaching at the *browser* level (rather than to a page) is what lets us open our own
    tab, which matters on these handsets: the farm phones carry hundreds of tabs from
    other studies, and Chrome on Android freezes backgrounded ones, so reusing an existing
    tab means talking to a renderer that may never answer.
    """

    def __init__(self, ws_url: str, response_filter: tuple[str, ...] = ()):
        self._ws_url = ws_url
        self._filter = response_filter
        self._ws: Any = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        # (session_id, request_id, url) for every response matching `response_filter`.
        self.responses: list[tuple[str, str, str]] = []

    async def __aenter__(self) -> "CdpSession":
        # 100 MB: a single item_list page came back at ~1 MB, but video payloads and
        # long comment pages run much larger, and a truncated frame kills the connection.
        # No keepalive pings: every call carries its own timeout, and a ping that a slow
        # farm host delays past websockets' 20s default would drop a healthy session.
        self._ws = await websockets.connect(self._ws_url, max_size=100 * 1024 * 1024,
                                            ping_interval=None)
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(PhoneError(f"CDP error: {msg['error']}"))
                        else:
                            fut.set_result(msg.get("result", {}))
                elif msg.get("method") == "Network.responseReceived":
                    params = msg["params"]
                    url = params["response"]["url"]
                    if any(ep in url for ep in self._filter):
                        self.responses.append(
                            (msg.get("sessionId", ""), params["requestId"], url))
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001 — a dead socket must fail every waiter
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(PhoneError(f"CDP connection lost: {ex}"))
            self._pending.clear()

    async def call(self, method: str, params: Optional[dict] = None,
                   session_id: Optional[str] = None, timeout: float = 30.0) -> dict:
        self._id += 1
        payload: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(self._id, None)
            raise PhoneError(f"CDP command {method} timed out after {timeout}s")

    async def open_tab(self, url: str = "about:blank") -> tuple[str, str]:
        """Create a tab, attach to it, enable the domains we read, foreground it.

        Returns (target_id, session_id). `Page.bringToFront` is not cosmetic: Android
        Chrome throttles background tabs to the point that navigation never completes.
        """
        target_id = (await self.call("Target.createTarget", {"url": url}))["targetId"]
        session_id = (await self.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}))["sessionId"]
        for domain in ("Page", "Network", "Runtime"):
            await self.call(f"{domain}.enable", {}, session_id=session_id)
        await self.call("Page.bringToFront", {}, session_id=session_id)
        return target_id, session_id

    async def close_tab(self, target_id: str, session_id: Optional[str] = None) -> None:
        """Best effort: a tab we cannot close is untidy, not a failed scrape.

        Drops that tab's captured responses too, so a long run over many profiles does not
        accumulate the whole crawl's wire traffic in memory.
        """
        try:
            await self.call("Target.closeTarget", {"targetId": target_id}, timeout=10)
        except Exception as ex:  # noqa: BLE001
            logger.debug(f"could not close phone tab {target_id}: {ex}")
        if session_id:
            self.responses = [r for r in self.responses if r[0] != session_id]

    async def evaluate(self, expression: str, session_id: str, timeout: float = 30.0) -> Any:
        res = await self.call("Runtime.evaluate",
                              {"expression": expression, "returnByValue": True,
                               "awaitPromise": True},
                              session_id=session_id, timeout=timeout)
        if "exceptionDetails" in res:
            raise PhoneError(f"JS evaluation failed: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")

    async def response_body(self, request_id: str, session_id: str) -> Optional[dict]:
        """Parsed JSON body of a captured response, or None if Chrome no longer has it.

        Bodies are evicted once the renderer garbage-collects them, and an empty body is
        TikTok's own way of refusing a request, so neither case is an error here.
        """
        try:
            res = await self.call("Network.getResponseBody", {"requestId": request_id},
                                  session_id=session_id, timeout=30)
        except PhoneError as ex:
            logger.debug(f"response body unavailable for {request_id}: {ex}")
            return None
        body = res.get("body") or ""
        if not body.strip():
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            logger.debug(f"response body for {request_id} was not JSON ({len(body)} bytes)")
            return None


# ── the scraper ───────────────────────────────────────────────────────────────

# One round trip: everything we want to know about the loaded profile page. Returned as a
# string so one `returnByValue` carries it whole.
_PAGE_STATE_JS = r"""
(() => {
  const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
  const text = document.body ? document.body.innerText : '';
  return JSON.stringify({
    url: location.href,
    rehydration: el ? el.textContent : null,
    text: text.slice(0, 2000),
    captcha: !!document.querySelector('[id*=captcha], [class*=captcha]'),
  });
})()
"""


class PhoneTikTok:
    """Drive one Android handset's Chrome to read TikTok mobile web.

    Args:
        serial: the phone's adb serial (see `list_serials`).
        ssh_host: SSH host the phone hangs off; None drives a locally attached phone.
            Falls back to env `PYTOK_PHONE_SSH`.
        adb: path to adb on that host. Falls back to env `PYTOK_ADB`, else "adb".
            On the MEO farm laptop that is ``C:\\platform-tools\\adb.exe``.
        cdp_port: local+remote TCP port for the DevTools forward. Give concurrent
            handsets different ports.
        page_timeout_s: how long to wait for a profile page to become readable. A
            budget, not a sleep -- a page that loads fast is read as soon as it does.
        listing_timeout_s: how long to wait for the profile's own `item_list` request
            to come back before treating the listing as failed.
        keep_awake: hold the screen on for the session. Chrome will not open its
            DevTools socket while the phone sleeps, so leaving this off means driving
            a phone something else is keeping awake.

    Use as an async context manager and reuse one instance across profiles::

        async with PhoneTikTok("R5CY23A5E7Y", ssh_host="meo-laptop") as phone:
            info = await phone.user_info("therock")
    """

    def __init__(self, serial: str, *, ssh_host: Optional[str] = None,
                 adb: Optional[str] = None, cdp_port: int = DEFAULT_CDP_PORT,
                 page_timeout_s: float = 45.0, listing_timeout_s: float = 45.0,
                 keep_awake: bool = True, sleep_on_exit: bool = False):
        self.serial = serial
        self._adb = AsyncAdb(adb=adb or os.getenv("PYTOK_ADB", "adb"),
                             ssh_host=ssh_host or os.getenv("PYTOK_PHONE_SSH") or None)
        self.cdp_port = cdp_port
        self.page_timeout_s = page_timeout_s
        self.listing_timeout_s = listing_timeout_s
        self.keep_awake = keep_awake
        self.sleep_on_exit = sleep_on_exit
        self._tunnel: Optional[CdpTunnel] = None
        self._cdp: Optional[CdpSession] = None
        # set by from_pool: the pool account bound to this phone, released on exit
        self.account = None
        self._pool = None

    @classmethod
    async def from_pool(cls, accounts_pool, serial: str, **kwargs) -> "PhoneTikTok":
        """A PhoneTikTok for `serial`, holding the pool account bound to that phone.

        Bind accounts to phones with `python -m pytok.accounts.cli bind-phone`. The
        account is marked in use until the session exits, so a desktop session cannot
        take it meanwhile. On entry the session checks which account the phone's
        Chrome is logged into: another account is an error, logged out a warning.

        Use as ``async with await PhoneTikTok.from_pool(pool, serial) as phone:``.
        """
        account = await accounts_pool.get_for_phone(serial)
        if account is None:
            bound = (await accounts_pool.phone_bindings()).get(serial)
            if bound is None:
                raise PhoneError(f"No account is bound to phone {serial} "
                                 f"(python -m pytok.accounts.cli bind-phone {serial} <username>)")
            raise PhoneError(f"{bound}, bound to phone {serial}, is in use by another session")
        phone = cls(serial, **kwargs)
        phone.account = account
        phone._pool = accounts_pool
        return phone

    # -- lifecycle ------------------------------------------------------------

    async def __aenter__(self) -> "PhoneTikTok":
        if self.keep_awake:
            await self.wake()
        await self._ensure_chrome()
        # Chrome stalls a tab while another app is in front, which a farm phone often has.
        await self._adb.shell(self.serial, f"am start -n {CHROME_PKG}/{_CHROME_MAIN}")
        self._tunnel = CdpTunnel(self._adb, self.serial, self.cdp_port)
        await self._tunnel.__aenter__()
        # From here on, anything that fails has to undo the tunnel by hand: `async with`
        # does not call __aexit__ when __aenter__ raises, and the adb forward would
        # outlive the process that made it.
        try:
            version = await self._tunnel.get_json("/json/version")
            ws_url = version["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1")
            logger.info(f"[phone {self.serial}] driving {version.get('Browser')}")
            self._cdp = CdpSession(ws_url, response_filter=(_ITEM_LIST_EP, _RECOMMEND_EP))
            await self._cdp.__aenter__()
            if self.account is not None:
                await self._check_identity()
        except Exception:
            if self._cdp is not None:
                await self._cdp.__aexit__(None, None, None)
                self._cdp = None
            await self._tunnel.__aexit__(None, None, None)
            self._tunnel = None
            await self._release_account()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._cdp is not None:
            await self._cdp.__aexit__(*exc)
        if self._tunnel is not None:
            await self._tunnel.__aexit__(*exc)
        if self.sleep_on_exit:
            await self._adb.shell(self.serial, "svc power stayon false; input keyevent KEYCODE_SLEEP")
        await self._release_account()

    async def _release_account(self) -> None:
        if self._pool is not None and self.account is not None:
            await self._pool.release_account(self.account.username)
            self._pool = None

    async def login(self, username: str, password: str, timeout: float = 90.0) -> str:
        """Log this phone's Chrome into TikTok with an email/username and password.

        Types into mobile web's login form through CDP, so the page's own input
        handlers fire as they would for a person typing. Returns the handle now logged
        in. When TikTok asks to verify the login, the emailed code is read from the
        phone's Gmail, which must be signed into the account's address. A captcha or a
        login error raises rather than being worked around: both need a person.
        """
        if self._cdp is None:
            raise PhoneError("PhoneTikTok used outside its `async with` block")
        cdp = self._cdp
        target_id, session_id = await cdp.open_tab()
        try:
            await cdp.call("Page.navigate", {"url": LOGIN_URL}, session_id=session_id, timeout=90)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.page_timeout_s
            while not await cdp.evaluate(
                    f"!!document.querySelector({json.dumps(_LOGIN_FIELDS[0])})", session_id):
                if loop.time() > deadline:
                    raise PhoneError(f"TikTok's login form never appeared on phone {self.serial}")
                await asyncio.sleep(1.0)

            for selector, value in zip(_LOGIN_FIELDS, (username, password), strict=True):
                await cdp.evaluate(
                    f"(() => {{ const el = document.querySelector({json.dumps(selector)}); "
                    f"el.focus(); el.select(); }})()", session_id)
                await cdp.call("Input.insertText", {"text": value}, session_id=session_id)
                await asyncio.sleep(0.5)
            await cdp.evaluate(
                "document.querySelector('[data-e2e=\"login-button\"]').click()", session_id)

            deadline = loop.time() + timeout
            code_entered = False
            while loop.time() < deadline:
                await asyncio.sleep(2.0)
                cookies = (await cdp.call("Network.getCookies",
                                          {"urls": ["https://www.tiktok.com"]},
                                          session_id=session_id)).get("cookies", [])
                if _SESSION_COOKIES & {c["name"] for c in cookies}:
                    break
                if not code_entered and await cdp.evaluate(
                        "/Verify it.s really you/.test(document.body.innerText)", session_id):
                    await self._enter_email_code(session_id)
                    code_entered = True
                    deadline = loop.time() + timeout
                    continue
                obstacle = await cdp.evaluate(_LOGIN_OBSTACLE_JS, session_id)
                if obstacle and obstacle.startswith("captcha"):
                    raise CaptchaException(
                        f"TikTok asked phone {self.serial} for a {obstacle} while logging "
                        f"in as {username}")
                if obstacle:
                    raise PhoneError(f"login as {username} on phone {self.serial} failed: {obstacle}")
            else:
                text = await cdp.evaluate("document.body.innerText.slice(-300)", session_id)
                raise PhoneError(f"login as {username} on phone {self.serial} did not complete "
                                 f"within {timeout:.0f}s; the page ends: {text!r}")
        finally:
            await cdp.close_tab(target_id, session_id)

        handle = await self.logged_in_unique_id()
        if not handle:
            raise PhoneError(f"phone {self.serial} got session cookies for {username} but "
                             f"TikTok shows no logged-in user")
        logger.info(f"[phone {self.serial}] logged in as @{handle}")
        return handle

    async def _enter_email_code(self, session_id: str) -> None:
        """Get past "verify it's really you" with the code TikTok emails.

        The phone is signed into the account's Gmail, but Gmail does not post a
        notification for these, so the code is read off the Gmail inbox list. Codes
        already in the inbox are noted first, so only the one this login asked for is used.
        """
        seen = {row for row, _ in await self._gmail_tiktok_codes()}
        await self._show_tab(session_id)
        if not await self._cdp.evaluate(_CHOOSE_EMAIL_METHOD_JS, session_id):
            raise PhoneError(f"phone {self.serial}: TikTok asked to verify the login but "
                             f"offered no email method")
        logger.info(f"[phone {self.serial}] TikTok asked to verify the login; reading the "
                    f"emailed code from Gmail")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _EMAIL_CODE_TIMEOUT
        code = None
        while code is None:
            if loop.time() > deadline:
                await self._show_tab(session_id)
                raise PhoneError(f"phone {self.serial}: TikTok's login code never reached "
                                 f"Gmail within {_EMAIL_CODE_TIMEOUT:.0f}s")
            await asyncio.sleep(8.0)
            fresh = [c for row, c in await self._gmail_tiktok_codes() if row not in seen]
            code = fresh[0] if fresh else None
        await self._show_tab(session_id)
        await self._cdp.evaluate(
            f"(() => {{ const el = document.querySelector({json.dumps(_CODE_INPUT)}); "
            f"el.focus(); el.select(); }})()", session_id)
        await self._cdp.call("Input.insertText", {"text": code}, session_id=session_id)
        await asyncio.sleep(3.0)
        # some variants of the step submit on the sixth digit, others want a tap
        await self._cdp.evaluate(_SUBMIT_CODE_JS, session_id)

    async def _gmail_tiktok_codes(self, attempts: int = 5) -> list[tuple[str, str]]:
        """(inbox row, code) for TikTok's code emails in the phone's Gmail, newest first.

        uiautomator refuses to dump a screen that is still animating, so a dump that
        comes back without a view hierarchy is retried rather than read as "no codes".
        """
        launch = f"monkey -p {GMAIL_PKG} -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1"
        for _ in range(attempts):
            xml = await self._adb.shell(
                self.serial,
                f"{launch}; sleep 4; uiautomator dump /sdcard/pytok_ui.xml >/dev/null; "
                f"cat /sdcard/pytok_ui.xml; rm -f /sdcard/pytok_ui.xml")
            if "<hierarchy" in xml:
                return list(dict.fromkeys(_TIKTOK_CODE_ROW_RE.findall(xml)))
            await asyncio.sleep(2.0)
        raise PhoneError(f"phone {self.serial}: could not read the Gmail inbox")

    async def _show_tab(self, session_id: str) -> None:
        """Put Chrome, and this tab within it, back in front."""
        await self._adb.shell(self.serial, f"am start -n {CHROME_PKG}/{_CHROME_MAIN}")
        await self._cdp.call("Page.bringToFront", {}, session_id=session_id)
        await asyncio.sleep(2.0)

    async def import_cookies(self, cookies: list[dict]) -> int:
        """Load saved TikTok cookies (the pool's Playwright-shaped backup) into Chrome.

        Returns how many were set. Only TikTok's own domains are copied.
        """
        if self._cdp is None:
            raise PhoneError("PhoneTikTok used outside its `async with` block")
        params = []
        for c in cookies:
            domain = c.get("domain") or ""
            if not domain.lstrip(".").endswith("tiktok.com"):
                continue
            cookie = {"name": c["name"], "value": c["value"], "path": c.get("path") or "/",
                      "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly"))}
            if domain.startswith("."):
                cookie["domain"] = domain
            else:
                # a domain attribute would widen a host-only cookie to its subdomains
                cookie["url"] = f"https://{domain}{cookie['path']}"
            if c.get("sameSite") in ("Strict", "Lax", "None"):
                cookie["sameSite"] = c["sameSite"]
            if c.get("expires") and c["expires"] > 0:
                cookie["expires"] = c["expires"]
            params.append(cookie)
        target_id, session_id = await self._cdp.open_tab()
        try:
            await self._cdp.call("Network.setCookies", {"cookies": params}, session_id=session_id)
        finally:
            await self._cdp.close_tab(target_id, session_id)
        return len(params)

    async def logged_in_unique_id(self) -> Optional[str]:
        """The handle of the account this phone's Chrome is logged into, or None."""
        target_id, session_id = await self._cdp.open_tab()
        try:
            await self._cdp.call("Page.navigate", {"url": FOR_YOU_URL},
                                 session_id=session_id, timeout=90)
            state = await self._await_page_state(session_id)
        finally:
            await self._cdp.close_tab(target_id, session_id)
        try:
            scope = json.loads(state.get("rehydration") or "{}").get("__DEFAULT_SCOPE__", {})
        except json.JSONDecodeError:
            return None
        user = (scope.get("webapp.app-context") or {}).get("user") or {}
        return user.get("uniqueId") or None

    async def _check_identity(self) -> None:
        handle = await self.logged_in_unique_id()
        expected = self.account.unique_id
        if handle is None:
            logger.warning(
                f"[phone {self.serial}] Chrome is not logged into TikTok, so reads are "
                f"anonymous rather than as {expected or self.account.username}")
        elif expected and handle.lower() != expected.lower():
            raise PhoneError(
                f"phone {self.serial}'s Chrome is logged in as @{handle}, not as its bound "
                f"account @{expected}")
        else:
            logger.info(f"[phone {self.serial}] logged in as @{handle}")

    async def wake(self, attempts: int = 3) -> None:
        """Wake and unlock the screen, and hold it on for the session.

        From Samsung's always-on display WAKEUP alone often leaves the panel dozing, and
        a dozing phone silently drops touches; HOME brings it fully on. Callers put
        Chrome back in front afterwards.
        """
        for _ in range(attempts):
            state = await self._adb.shell(
                self.serial,
                "svc power stayon true; input keyevent KEYCODE_WAKEUP; "
                "input keyevent KEYCODE_HOME; wm dismiss-keyguard; sleep 1; "
                "dumpsys power | grep -m1 mWakefulness=")
            if "Awake" in state:
                return
        raise PhoneError(f"phone {self.serial} would not wake (power state: {state.strip()})")

    async def _ensure_chrome(self, timeout: float = 45.0) -> None:
        """Start Chrome and wait for its DevTools socket to exist.

        Launching on about:blank rather than straight at the profile keeps this step
        independent of what we are about to scrape, so a failure here is unambiguously
        "the phone would not give us a browser".
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        launched = False
        while loop.time() < deadline:
            sockets = await self._adb.shell(
                self.serial, "cat /proc/net/unix | grep chrome_devtools_remote")
            if "chrome_devtools_remote" in sockets:
                return
            if not launched:
                await self._adb.shell(
                    self.serial,
                    f"am start -a android.intent.action.VIEW -d 'about:blank' {CHROME_PKG}")
                launched = True
            await asyncio.sleep(1.0)
        raise PhoneError(
            f"Chrome's DevTools socket never appeared on {self.serial} after {timeout:.0f}s "
            f"-- is Chrome installed, USB debugging authorized, and the screen awake?")

    # -- scraping -------------------------------------------------------------

    async def _load_profile(self, username: str) -> tuple[str, str, dict]:
        """Open a tab at a profile and return (target_id, session_id, page state)."""
        if self._cdp is None:
            raise PhoneError("PhoneTikTok used outside its `async with` block")
        url = PROFILE_URL.format(username=username)
        target_id, session_id = await self._cdp.open_tab()
        try:
            await self._cdp.call("Page.navigate", {"url": url},
                                 session_id=session_id, timeout=90)
            state = await self._await_page_state(session_id)
        except Exception:
            await self._cdp.close_tab(target_id, session_id)
            raise
        if state.get("captcha"):
            await self._cdp.close_tab(target_id, session_id)
            raise CaptchaException(f"phone {self.serial} was served a captcha for @{username}")
        return target_id, session_id, state

    async def _await_page_state(self, session_id: str) -> dict:
        """Poll the loaded page until its rehydration payload appears.

        Polling rather than sleeping a flat interval, because how long a profile takes to
        become readable varies by more than an order of magnitude across handsets: a phone
        carrying hundreds of other tabs starts its renderer slowly, and one fixed wait
        either gives up too early there or burns the difference on an idle phone.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.page_timeout_s
        state: dict = {}
        while loop.time() < deadline:
            raw = await self._cdp.evaluate(_PAGE_STATE_JS, session_id)
            state = json.loads(raw) if raw else {}
            if state.get("rehydration") or state.get("captcha"):
                return state
            await asyncio.sleep(1.0)
        return state

    async def _await_listing(self, session_id: str) -> bool:
        """Wait for this tab's `item_list` request to come back. False if it never did."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.listing_timeout_s
        while loop.time() < deadline:
            if any(sid == session_id for sid, _, _ in self._cdp.responses):
                return True
            await asyncio.sleep(0.5)
        return False

    @staticmethod
    def _user_from_rehydration(state: dict, username: str) -> dict:
        """Pull the user out of the page's rehydration blob, in `User.info()`'s shape.

        Mobile web ships the same `__UNIVERSAL_DATA_FOR_REHYDRATION__` payload the desktop
        page does, so this returns `{**user, **stats}` exactly as `_info_full_scrape` does
        and the result drops into `utils.get_user_df` unchanged.
        """
        blob = state.get("rehydration")
        if not blob:
            text = state.get("text", "")
            if "Couldn't find this account" in text:
                raise NotFoundException(f"TikTok says @{username} does not exist")
            raise ApiFailedException(
                f"no rehydration data on the mobile profile page for @{username} "
                f"(page text began: {text[:120]!r})")
        try:
            scope = json.loads(blob)["__DEFAULT_SCOPE__"]
        except (json.JSONDecodeError, KeyError) as ex:
            raise ApiFailedException(f"malformed rehydration data for @{username}: {ex}")

        detail = scope.get("webapp.user-detail", {})
        status = detail.get("statusCode")
        if status in (10202, 10221, 100002):
            raise NotFoundException(
                f"TikTok indicated @{username} does not exist: statusCode={status}")
        if status not in (0, None):
            raise ApiFailedException(
                f"TikTok returned statusCode={status} for @{username} on mobile web")

        user_info = detail.get("userInfo", {})
        user = {**user_info.get("user", {}), **user_info.get("stats", {})}
        if not user.get("id"):
            raise ApiFailedException(f"no user data in the mobile page payload for @{username}")
        return user

    async def user_info(self, username: str) -> dict:
        """Profile info for one user, in the same shape as `User.info()`.

        Reads the page's own rehydration payload rather than issuing an API call, so this
        costs exactly one page load and needs no request signing.
        """
        target_id, session_id, state = await self._load_profile(username)
        try:
            user = self._user_from_rehydration(state, username)
            logger.info(f"[phone {self.serial}] @{username}: "
                        f"{user.get('followerCount')} followers, "
                        f"{user.get('videoCount')} videos")
            return user
        finally:
            await self._cdp.close_tab(target_id, session_id)

    async def user_videos(self, username: str, count: Optional[int] = None) -> list[dict]:
        """The user's most recent videos, as raw `itemList` dicts.

        Returns what the profile's own first `item_list` request returned -- the dicts go
        straight into `utils.get_video_df`. Mobile web fetches one page (~35 videos) and
        does not paginate further; see the module docstring for why going deeper needs
        request signing. When the listing is capped rather than exhausted this logs the
        ceiling it hit, so a short result is never mistaken for a short profile.
        """
        target_id, session_id, state = await self._load_profile(username)
        try:
            # An empty profile is a legitimate answer, but a *blocked* one also looks
            # empty, so cross-check against the count the profile itself reports -- the
            # same discriminator `User._iter_videos` uses.
            try:
                expected = self._user_from_rehydration(state, username).get("videoCount")
            except (ApiFailedException, NotFoundException):
                expected = None

            await self._await_listing(session_id)

            # Only this tab's responses: one instance scrapes many profiles in a row, and
            # the capture list is per-connection, so matching on the session keeps the
            # previous profile's listing out of this one's.
            captured = [(rid, url) for sid, rid, url in self._cdp.responses
                        if sid == session_id]
            videos: list[dict] = []
            seen: set[str] = set()
            has_more = None
            readable = 0
            for request_id, _url in captured:
                body = await self._cdp.response_body(request_id, session_id)
                if not body:
                    continue
                readable += 1
                has_more = body.get("hasMore", has_more)
                for item in body.get("itemList") or []:
                    if item.get("id") and item["id"] not in seen:
                        seen.add(item["id"])
                        videos.append(item)

            if not videos:
                # Three different things look like "no videos" here, and they call for
                # different responses from a caller, so say which one happened. The
                # empty-body case is the common one: TikTok answers a listing it is
                # throttling with a 200 and nothing in it.
                if not captured:
                    raise ApiFailedException(
                        f"@{username}'s profile page never requested its video listing "
                        f"within {self.listing_timeout_s:.0f}s -- the page did not finish "
                        f"loading on phone {self.serial}")
                if not readable:
                    raise ApiFailedException(
                        f"TikTok answered the video listing for @{username} with an empty "
                        f"body -- the request was refused rather than the profile being "
                        f"empty (commonly rate-limiting; phones on one WiFi share an "
                        f"egress IP, so backing off is per-IP not per-phone)")
                if expected:
                    raise ApiFailedException(
                        f"no videos returned for @{username} though the profile reports "
                        f"{expected} -- the mobile listing failed rather than being empty")
                logger.info(f"[phone {self.serial}] @{username} has no videos")
                return []

            if count is not None:
                videos = videos[:count]
            elif has_more:
                logger.info(
                    f"[phone {self.serial}] @{username}: returning {len(videos)} videos but "
                    f"TikTok reports more"
                    + (f" (profile says {expected})" if expected else "")
                    + " -- mobile web serves one item_list page and cannot paginate further")
            return videos
        finally:
            await self._cdp.close_tab(target_id, session_id)

    # -- home feed ------------------------------------------------------------

    async def for_you(self, count: int = 30) -> list[dict]:
        """Videos from the mobile For You feed, as raw `itemList` dicts.

        Swipes through the feed in its own tab with CDP touch events, which reach only
        that tab, and collects the batches the page fetches as it goes. The walk ends at
        `count` distinct videos, once swiping stops bringing new ones, or once the feed
        stops moving -- which mobile web does after its first batch.
        """
        if self._cdp is None:
            raise PhoneError("PhoneTikTok used outside its `async with` block")
        cdp = self._cdp
        target_id, session_id = await cdp.open_tab()
        try:
            await cdp.call("Page.navigate", {"url": FOR_YOU_URL},
                           session_id=session_id, timeout=90)
            videos: list[dict] = []
            seen: set[str] = set()
            read = 0

            async def collect() -> int:
                nonlocal read
                mine = [(rid, url) for sid, rid, url in cdp.responses
                        if sid == session_id and _RECOMMEND_EP in url]
                added = 0
                for request_id, _url in mine[read:]:
                    body = await cdp.response_body(request_id, session_id)
                    for item in (body or {}).get("itemList") or []:
                        if item.get("id") and item["id"] not in seen:
                            seen.add(item["id"])
                            videos.append(item)
                            added += 1
                read = len(mine)
                return added

            if not await self._await_recommendations(session_id):
                raise ApiFailedException(
                    f"the For You page on phone {self.serial} sent no recommendations "
                    f"within {self.listing_timeout_s:.0f}s")
            await collect()
            if not videos:
                raise ApiFailedException(
                    "TikTok answered the For You feed with an empty body -- refused rather "
                    "than empty (commonly rate-limiting, which is per egress IP)")
            width, height = await cdp.evaluate("[innerWidth, innerHeight]", session_id)

            idle = 0
            stuck = 0
            touch_failures = 0
            active = await cdp.evaluate(_ACTIVE_SLIDE_JS, session_id)
            while len(videos) < count and idle < _FOR_YOU_STALL_SWIPES:
                try:
                    await asyncio.wait_for(self._dismiss_app_prompt(session_id), _TOUCH_TIMEOUT)
                    await asyncio.wait_for(
                        self._swipe_up(session_id, width, height), _TOUCH_TIMEOUT)
                    touch_failures = 0
                except (asyncio.TimeoutError, PhoneError) as ex:
                    touch_failures += 1
                    if touch_failures > _TOUCH_FAILURES_ALLOWED:
                        raise PhoneError(
                            f"phone {self.serial} stopped responding to touch in the feed "
                            f"tab ({ex}) -- it is asleep, or another job has its own tab "
                            f"or app in front")
                    # close the half-finished gesture so the next touchStart begins clean
                    try:
                        await cdp.call("Input.dispatchTouchEvent",
                                       {"type": "touchCancel", "touchPoints": []},
                                       session_id=session_id, timeout=5)
                    except PhoneError:
                        pass
                    await self.wake()
                    await self._show_tab(session_id)
                    continue
                # the time a viewer spends on a video, and the page's chance to fetch
                await asyncio.sleep(2.0)
                added = await collect()
                idle = 0 if added else idle + 1
                logger.debug(f"[phone {self.serial}] For You swipe: {added} new, {len(videos)} so far")

                await self._dismiss_app_prompt(session_id)
                now_active = await cdp.evaluate(_ACTIVE_SLIDE_JS, session_id)
                stuck = stuck + 1 if now_active == active else 0
                active = now_active
                if stuck >= _FOR_YOU_STUCK_SWIPES:
                    logger.warning(
                        f"[phone {self.serial}] For You feed stopped moving after "
                        f"{len(videos)} videos; mobile web stops paging the feed after its "
                        f"first batch")
                    break

            logger.info(f"[phone {self.serial}] For You: {len(videos)} videos")
            return videos[:count]
        finally:
            await cdp.close_tab(target_id, session_id)

    async def _await_recommendations(self, session_id: str) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.listing_timeout_s
        while loop.time() < deadline:
            if any(sid == session_id and _RECOMMEND_EP in url
                   for sid, _, url in self._cdp.responses):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _dismiss_app_prompt(self, session_id: str) -> None:
        if await self._cdp.evaluate(_DISMISS_APP_PROMPT_JS, session_id):
            await asyncio.sleep(0.5)

    async def _swipe_up(self, session_id: str, width: int, height: int) -> None:
        """One upward swipe, moving the feed on by a video."""
        x, start, end = width // 2, int(height * 0.75), int(height * 0.25)
        steps = 8
        await self._cdp.call("Input.dispatchTouchEvent",
                             {"type": "touchStart", "touchPoints": [{"x": x, "y": start}]},
                             session_id=session_id)
        for k in range(1, steps + 1):
            y = start + (end - start) * k // steps
            await self._cdp.call("Input.dispatchTouchEvent",
                                 {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]},
                                 session_id=session_id)
            await asyncio.sleep(0.02)
        await self._cdp.call("Input.dispatchTouchEvent",
                             {"type": "touchEnd", "touchPoints": []}, session_id=session_id)
