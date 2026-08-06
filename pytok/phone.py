"""Scrape TikTok mobile web by driving Chrome on a physical Android phone over adb + CDP.

A third data route, alongside the two in `tiktok.py` (TikTok-Api requests, and zendriver
driving a desktop Chrome). Instead of a browser on this machine it drives **Chrome on a
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
then stops: there is no infinite scroll to drive, and the response URL is signed
(`msToken` + `X-Bogus` + `X-Gnarly`) over its own query string, so replaying it with an
advanced cursor returns a 200 with an empty body. Going deeper needs the signing
machinery in `tiktok_api.py` driven from inside the page, which this module does not do
yet -- `user_videos()` documents its own ceiling and says when it hit it.

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
DEFAULT_CDP_PORT = 9222
PROFILE_URL = "https://www.tiktok.com/@{username}"

# The endpoint worth keeping when it goes past on the wire. Substring match on the URL,
# because it carries a long signed query string. Profile info does not need capturing:
# the mobile page ships it in its rehydration payload and never calls /api/user/detail/.
_ITEM_LIST_EP = "/api/post/item_list/"


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
            # Windows keepalive, killed when we terminate the ssh process. The forward and
            # the -L tunnel must share one process (gotcha 1 above).
            keepalive = "ping -n 100000 127.0.0.1 >NUL"
            self._proc = await asyncio.create_subprocess_exec(
                "ssh", "-L", f"{self._port}:127.0.0.1:{self._port}",
                self._adb.ssh_host, f"{fwd} && {keepalive}",
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

    async def _wait_ready(self, timeout: float = 25.0) -> None:
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
        self._ws = await websockets.connect(self._ws_url, max_size=100 * 1024 * 1024)
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

    # -- lifecycle ------------------------------------------------------------

    async def __aenter__(self) -> "PhoneTikTok":
        if self.keep_awake:
            await self.wake()
        await self._ensure_chrome()
        self._tunnel = CdpTunnel(self._adb, self.serial, self.cdp_port)
        await self._tunnel.__aenter__()
        # From here on, anything that fails has to undo the tunnel by hand: `async with`
        # does not call __aexit__ when __aenter__ raises, and the adb forward would
        # outlive the process that made it.
        try:
            version = await self._tunnel.get_json("/json/version")
            ws_url = version["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1")
            logger.info(f"[phone {self.serial}] driving {version.get('Browser')}")
            self._cdp = CdpSession(ws_url, response_filter=(_ITEM_LIST_EP,))
            await self._cdp.__aenter__()
        except Exception:
            await self._tunnel.__aexit__(None, None, None)
            self._tunnel = None
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._cdp is not None:
            await self._cdp.__aexit__(*exc)
        if self._tunnel is not None:
            await self._tunnel.__aexit__(*exc)
        if self.sleep_on_exit:
            await self._adb.shell(self.serial, "svc power stayon false; input keyevent KEYCODE_SLEEP")

    async def wake(self) -> None:
        """Wake and unlock the screen, and hold it on for the session."""
        await self._adb.shell(
            self.serial,
            "input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard; svc power stayon true")

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
