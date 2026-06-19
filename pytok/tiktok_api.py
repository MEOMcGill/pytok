"""Standalone TikTok API client backed by zendriver.

Manages sessions (tabs) within a shared zendriver browser to make
signed API requests to TikTok.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import random
import time
from typing import Any, Optional
from urllib.parse import urlencode, quote, urlparse

from zendriver import cdp
from zendriver.core.connection import ProtocolException

from .exceptions import InvalidJSONException, EmptyResponseException


@dataclasses.dataclass
class TikTokSession:
    """A TikTok session backed by a zendriver tab."""

    tab: Any
    proxy: str = None
    params: dict = None
    headers: dict = None
    ms_token: str = None
    base_url: str = "https://www.tiktok.com"
    is_valid: bool = True



def _random_choice(choices):
    """Pick a random element from choices, or return None if empty."""
    if choices is None or len(choices) == 0:
        return None
    return random.choice(choices)


class ZendriverTikTokApi:
    """TikTok API client backed by a shared zendriver browser.

    Manages sessions (tabs) within a zendriver browser owned by PyTok.
    """

    # webmssdk.js defines window.byted_acrawler.frontierSign (X-Bogus signing).
    # The URL is normally discovered from the live DOM so the version stays
    # current; this hardcoded version is a stale-prone last resort only.
    _SIGNING_SDK_URL_FALLBACK = (
        "https://sf16-website-login.neutral.ttwstatic.com/obj/"
        "tiktok_web_login_static/webmssdk/1.0.0.374/webmssdk.js"
    )

    def __init__(self, logging_level: int = logging.WARN, logger_name: str = None):
        self.sessions = []
        self._session_recovery_enabled = True
        self._session_creation_lock = asyncio.Lock()
        self._cleanup_called = False
        self._auto_cleanup_dead_sessions = True
        self._owns_browser = False
        self.browser = None
        # Cached webmssdk.js source, captured from a healthy session and
        # re-injected to self-heal sessions where the signer failed to load.
        self._signing_sdk_src = None

        if logger_name is None:
            logger_name = "ZendriverTikTokApi"
        self._create_logger(logger_name, logging_level)

    def _create_logger(self, name: str, level: int = logging.DEBUG):
        """Create a logger for the class."""
        self.logger: logging.Logger = logging.getLogger(name)
        self.logger.setLevel(level)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def __del__(self):
        if not self._cleanup_called:
            if self.sessions or self.browser:
                self.logger.warning(
                    "ZendriverTikTokApi object is being destroyed but cleanup was not called. "
                    f"Leaked resources: {len(self.sessions)} sessions, "
                    f"browser={'exists' if self.browser else 'none'}"
                )

    def _get_session(self, **kwargs):
        """Get a session by index or randomly."""
        if len(self.sessions) == 0:
            raise Exception("No sessions created, please create sessions first")
        if kwargs.get("session_index") is not None:
            i = kwargs["session_index"]
        else:
            i = random.randint(0, len(self.sessions) - 1)
        return i, self.sessions[i]

    # ------------------------------------------------------------------
    # Session params (merged from PatchedTikTokApi)
    # ------------------------------------------------------------------

    async def _set_session_params(self, session):
        """Override session params to match what browser actually sends."""
        user_agent = await session.tab.evaluate("navigator.userAgent")
        language = await session.tab.evaluate(
            "navigator.language || navigator.userLanguage"
        )
        platform = await session.tab.evaluate("navigator.platform")
        device_id = str(random.randint(10**18, 10**19 - 1))
        odin_id = str(random.randint(10**18, 10**19 - 1))
        history_len = str(random.randint(1, 10))
        screen_height = str(random.randint(600, 1080))
        screen_width = str(random.randint(800, 1920))
        web_id_last_time = str(int(time.time()))
        timezone = await session.tab.evaluate(
            "Intl.DateTimeFormat().resolvedOptions().timeZone"
        )
        browser_version = await session.tab.evaluate("navigator.appVersion")
        os_name = platform.lower().split()[0] if platform else "windows"

        session.params = {
            "WebIdLastTime": web_id_last_time,
            "aid": "1988",
            "app_language": language,
            "app_name": "tiktok_web",
            "browser_language": language,
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": platform,
            "browser_version": browser_version,
            "channel": "tiktok_web",
            "cookie_enabled": "true",
            "data_collection_enabled": "false",
            "device_id": device_id,
            "device_platform": "web_pc",
            "focus_state": "true",
            "from_page": "user",
            "history_len": history_len,
            "is_fullscreen": "false",
            "is_page_visible": "true",
            "language": language,
            "odinId": odin_id,
            "os": os_name,
            "region": "US",
            "screen_height": screen_height,
            "screen_width": screen_width,
            "tz_name": timezone,
            "user_is_login": "false",
            "video_encoding": "mp4",
            "webcast_language": language,
        }

    # ------------------------------------------------------------------
    # Session validation
    # ------------------------------------------------------------------

    async def _is_session_valid(self, session) -> bool:
        if not session.is_valid:
            return False
        try:
            if session.tab.closed:
                session.is_valid = False
                return False
            _ = session.tab.url
            return True
        except Exception as e:
            self.logger.warning(f"Session validation failed: {e}")
            session.is_valid = False
            return False

    async def _mark_session_invalid(self, session):
        session.is_valid = False
        try:
            if session.tab and not session.tab.closed:
                await session.tab.close()
        except Exception as e:
            self.logger.debug(f"Error closing tab during invalidation: {e}")

        if self._auto_cleanup_dead_sessions and session in self.sessions:
            try:
                self.sessions.remove(session)
                self.logger.debug(
                    f"Removed dead session from pool. Remaining: {len(self.sessions)}"
                )
            except ValueError:
                pass

    async def _get_valid_session_index(self, **kwargs):
        """Get a valid session, with automatic recovery if needed.

        Args:
            session_index (int, optional): Specific session index to use.

        Returns:
            tuple: (index, session)

        Raises:
            Exception: If no valid sessions available and recovery fails.
        """
        max_attempts = 3

        for attempt in range(max_attempts):
            if kwargs.get("session_index") is not None:
                i = kwargs["session_index"]
                if i < len(self.sessions):
                    session = self.sessions[i]
                    if await self._is_session_valid(session):
                        return i, session
                    else:
                        self.logger.warning(f"Requested session {i} is invalid")
            else:
                valid_sessions = []
                for idx, session in enumerate(self.sessions):
                    if await self._is_session_valid(session):
                        valid_sessions.append((idx, session))

                if valid_sessions:
                    return random.choice(valid_sessions)

            # No valid sessions found - attempt recovery if enabled
            if self._session_recovery_enabled and attempt < max_attempts - 1:
                self.logger.warning(
                    f"No valid sessions found, attempting recovery "
                    f"(attempt {attempt + 1}/{max_attempts})"
                )
                await self._recover_sessions()
            else:
                break

        raise Exception(
            "No valid sessions available. All sessions appear to be dead. "
            "Please call create_sessions() again or restart the API."
        )

    # ------------------------------------------------------------------
    # Polling helper (replaces page.wait_for_function)
    # ------------------------------------------------------------------

    async def _poll_for_condition(self, tab, js_condition, timeout=10, poll_interval=0.5):
        """Poll a JS condition until truthy or timeout (seconds)."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        while loop.time() - start < timeout:
            result = await tab.evaluate(js_condition)
            if result:
                return True
            await asyncio.sleep(poll_interval)
        raise asyncio.TimeoutError(
            f"Condition '{js_condition}' not met within {timeout}s"
        )

    # ------------------------------------------------------------------
    # Session creation
    # ------------------------------------------------------------------

    async def _create_session(
        self,
        url: str = "https://www.tiktok.com",
        ms_token: str | None = None,
        sleep_after: int = 1,
        cookies: dict[str, Any] | None = None,
        timeout: int = 30000,
    ):
        """Create a TikTokSession using the shared zendriver browser."""
        tab = None
        try:
            if ms_token is not None:
                if cookies is None:
                    cookies = {}
                cookies["msToken"] = ms_token

            # Set cookies via CDP before navigating
            if cookies is not None:
                domain = urlparse(url).netloc
                cookie_params = [
                    cdp.network.CookieParam(
                        name=k, value=v, domain=domain, path="/"
                    )
                    for k, v in cookies.items()
                    if v is not None
                ]
                await self.browser.cookies.set_all(cookie_params)

            # Open new tab in background (avoid stealing focus from main tab)
            target_id = await self.browser.connection.send(
                cdp.target.create_target(url, background=True)
            )
            tab = next(
                t for t in self.browser.targets
                if t.type_ == "page" and t.target_id == target_id
            )
            tab.browser = self.browser
            await asyncio.sleep(0.25)

            if "tiktok" not in tab.url:
                await tab.get("https://www.tiktok.com")

            # Capture request headers via CDP
            request_headers = None
            headers_event = asyncio.Event()

            def handle_request(event: cdp.network.RequestWillBeSent, connection=None):
                nonlocal request_headers
                if not isinstance(event, cdp.network.RequestWillBeSent):
                    return
                if request_headers is None:
                    raw = event.request.headers
                    request_headers = dict(raw) if raw else {}
                    headers_event.set()

            tab.add_handler(cdp.network.RequestWillBeSent, handle_request)

            # Simulate mouse movement to avoid bot detection
            x, y = random.randint(0, 50), random.randint(0, 50)
            a, b = random.randint(1, 50), random.randint(100, 200)

            await tab.send(cdp.input_.dispatch_mouse_event(
                type_="mouseMoved", x=x, y=y
            ))
            await tab.wait_for_ready_state(
                until="complete", timeout=max(timeout // 1000, 10)
            )
            await tab.send(cdp.input_.dispatch_mouse_event(
                type_="mouseMoved", x=a, y=b
            ))

            # Wait briefly for headers
            try:
                await asyncio.wait_for(headers_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

            tab.remove_handlers(cdp.network.RequestWillBeSent)

            session = TikTokSession(
                tab=tab,
                ms_token=ms_token,
                headers=request_headers,
                base_url=url,
                is_valid=True,
            )

            if ms_token is None:
                await asyncio.sleep(sleep_after)
                cookies_dict = await self.get_session_cookies(session)
                ms_token = cookies_dict.get("msToken")
                session.ms_token = ms_token
                if ms_token is None:
                    self.logger.info(
                        f"Failed to get msToken on session index {len(self.sessions)}, "
                        "you should consider specifying ms_tokens"
                    )

            self.sessions.append(session)
            await self._set_session_params(session)
        except Exception as e:
            self.logger.error(f"Failed to create session: {e}")
            if tab is not None:
                try:
                    await tab.close()
                except Exception:
                    pass
            raise

    async def create_sessions(
        self,
        zendriver_browser,
        num_sessions: int = 1,
        ms_tokens: list[str] | None = None,
        sleep_after: int = 1,
        starting_url: str = "https://www.tiktok.com",
        cookies: list[dict[str, Any]] | None = None,
        timeout: int = 30000,
        enable_session_recovery: bool = True,
        **kwargs,
    ):
        """Create sessions using a shared zendriver browser.

        Args:
            zendriver_browser: A zendriver Browser instance (required).
            num_sessions: Number of sessions (tabs) to create.
            ms_tokens: Optional list of msTokens.
            sleep_after: Seconds to sleep after session creation.
            starting_url: URL to start sessions on.
            cookies: Optional list of cookie dicts.
            timeout: Navigation timeout in milliseconds.
            enable_session_recovery: Enable automatic session recovery.
        """
        self._session_recovery_enabled = enable_session_recovery
        self.browser = zendriver_browser

        # Store creation params for session recovery
        self._creation_params = {
            "ms_tokens": ms_tokens,
            "sleep_after": sleep_after,
            "starting_url": starting_url,
            "cookies": cookies,
            "timeout": timeout,
        }

        await asyncio.gather(
            *(
                self._create_session(
                    ms_token=_random_choice(ms_tokens),
                    url=starting_url,
                    sleep_after=sleep_after,
                    cookies=_random_choice(cookies),
                    timeout=timeout,
                )
                for _ in range(num_sessions)
            )
        )

    async def _recover_sessions(self):
        """Recover from dead sessions by creating a new one."""
        async with self._session_creation_lock:
            self.logger.info("Starting session recovery...")

            # Remove invalid sessions
            initial_count = len(self.sessions)
            self.sessions = [
                s for s in self.sessions if await self._is_session_valid(s)
            ]
            removed_count = initial_count - len(self.sessions)
            if removed_count > 0:
                self.logger.info(f"Removed {removed_count} dead session(s)")

            # Create a replacement session using stored params
            if self.browser is not None and hasattr(self, "_creation_params"):
                p = self._creation_params
                try:
                    self.logger.info("Creating replacement session...")
                    await self._create_session(
                        url=p["starting_url"],
                        ms_token=_random_choice(p["ms_tokens"]),
                        sleep_after=p["sleep_after"],
                        cookies=_random_choice(p["cookies"]),
                        timeout=p["timeout"],
                    )
                    self.logger.info(
                        f"Session recovery successful. Active sessions: {len(self.sessions)}"
                    )
                except Exception as e:
                    self.logger.error(f"Failed to create replacement session: {e}")

    # ------------------------------------------------------------------
    # Session cleanup
    # ------------------------------------------------------------------

    async def close_sessions(self):
        """Close all session tabs. Does NOT stop the browser (we don't own it)."""
        self.logger.debug(f"Closing {len(self.sessions)} sessions...")

        for session in self.sessions:
            try:
                if session.tab and not session.tab.closed:
                    await session.tab.close()
            except Exception as e:
                self.logger.debug(f"Error closing tab: {e}")

        self.sessions.clear()
        self._cleanup_called = True
        self.logger.debug("All sessions closed successfully")

    async def stop_playwright(self):
        """No-op - we don't own the browser."""
        pass

    stop_browser = stop_playwright

    # ------------------------------------------------------------------
    # JS fetch
    # ------------------------------------------------------------------

    def generate_js_fetch(self, method: str, url: str, headers: dict) -> str:
        """Generate a JS fetch IIFE for zendriver evaluate."""
        # fetch() rejects a null headers value or non-string header values, so
        # coerce to a clean string->string dict (headers may be None for some
        # sessions, e.g. when loaded from a logged-in Chrome profile).
        clean_headers = {
            str(k): str(v) for k, v in (headers or {}).items() if v is not None
        }
        headers_js = json.dumps(clean_headers)
        return (
            f"(async () => {{"
            f"  const resp = await fetch('{url}', {{ method: '{method}', headers: {headers_js} }});"
            f"  return await resp.text();"
            f"}})()"
        )

    async def _evaluate(self, tab, expression, await_promise=False):
        """Evaluate JS, working around zendriver's falsy-value bug."""
        remote_object, errors = await tab.send(
            cdp.runtime.evaluate(
                expression=expression,
                user_gesture=True,
                await_promise=await_promise,
                return_by_value=True,
                allow_unsafe_eval_blocked_by_csp=True,
            )
        )
        if errors:
            raise ProtocolException(errors)
        if remote_object and remote_object.value is not None:
            return remote_object.value
        return None

    async def run_fetch_script(self, url: str, headers: dict, **kwargs):
        js_script = self.generate_js_fetch("GET", url, headers)

        try:
            _, session = await self._get_valid_session_index(**kwargs)
        except Exception:
            _, session = self._get_session(**kwargs)

        try:
            result = await self._evaluate(session.tab, js_script, await_promise=True)
            return result
        except Exception as e:
            self.logger.error(f"Session failed during fetch: {e}")
            await self._mark_session_invalid(session)
            raise

    # ------------------------------------------------------------------
    # Cookies
    # ------------------------------------------------------------------

    async def set_session_cookies(self, session, cookies):
        """Set cookies on the shared browser.

        Accepts either a list of cookie dicts (with name/value/domain/path keys)
        or a simple {name: value} dict.
        """
        if isinstance(cookies, dict):
            cookie_params = [
                cdp.network.CookieParam(
                    name=k, value=v, domain=".tiktok.com", path="/"
                )
                for k, v in cookies.items()
                if v is not None
            ]
        else:
            cookie_params = [
                cdp.network.CookieParam(
                    name=c["name"],
                    value=c["value"],
                    domain=c.get("domain", ".tiktok.com"),
                    path=c.get("path", "/"),
                )
                for c in cookies
            ]
        await self.browser.cookies.set_all(cookie_params)

    async def get_session_cookies(self, session):
        cookies = await self.browser.cookies.get_all()
        return {cookie.name: cookie.value for cookie in cookies}

    # ------------------------------------------------------------------
    # X-Bogus / signing
    # ------------------------------------------------------------------

    async def _discover_signing_sdk_url(self, session):
        """Find webmssdk.js's URL from the page DOM (keeps the version current)."""
        return await session.tab.evaluate(
            "(document.querySelector('script[src*=\"webmssdk/\"]') || {}).src || null"
        )

    async def _capture_signing_sdk(self, session):
        """Fetch and cache webmssdk.js from a healthy session (once).

        Captured from TikTok itself rather than vendored, so the signer stays
        version-matched. Reused to re-inject the signer into sessions where the
        SDK failed to load. Best-effort: failures are logged and ignored.
        """
        if self._signing_sdk_src is not None:
            return
        try:
            sdk_url = await self._discover_signing_sdk_url(session) \
                or self._SIGNING_SDK_URL_FALLBACK
            src = await session.tab.evaluate(
                f"fetch({json.dumps(sdk_url)}).then(r => r.text())",
                await_promise=True,
            )
            if src and len(src) > 1000:
                self._signing_sdk_src = src
                self.logger.debug(
                    f"Captured signing SDK ({len(src)} bytes) from {sdk_url}"
                )
        except Exception as e:
            self.logger.debug(f"Failed to capture signing SDK: {e}")

    async def _inject_signing_sdk(self, session) -> bool:
        """Re-inject the cached signer into a session via indirect eval.

        webmssdk only defines window.byted_acrawler when executed in global
        scope via indirect eval; a <script> tag or document-start injection
        does not work. Returns True if byted_acrawler is available afterwards.
        """
        if self._signing_sdk_src is None:
            return False
        try:
            # Stash on window first to avoid escaping a ~227KB source string
            # into an evaluate expression.
            await self._evaluate(
                session.tab,
                "window.__pytok_sdk__ = " + json.dumps(self._signing_sdk_src) + "; void 0",
            )
            await self._evaluate(session.tab, "(0,eval)(window.__pytok_sdk__)")
            present = await self._evaluate(
                session.tab, "window.byted_acrawler !== undefined"
            )
            if present:
                self.logger.info("Re-injected signing SDK into session via eval")
            return bool(present)
        except Exception as e:
            self.logger.debug(f"Failed to inject signing SDK: {e}")
            return False

    async def _reload_until_signer(self, session):
        """Legacy fallback: reload TikTok pages until byted_acrawler appears.

        Used only when no cached SDK is available to inject.
        """
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                timeout_time = random.randint(5000, 20000)
                await self._poll_for_condition(
                    session.tab,
                    "window.byted_acrawler !== undefined",
                    timeout=timeout_time / 1000,
                )
                return
            except asyncio.TimeoutError:
                if attempt == max_attempts:
                    raise asyncio.TimeoutError(
                        f"Failed to load tiktok after {max_attempts} attempts, "
                        "consider using a proxy"
                    )

                try_urls = [
                    "https://www.tiktok.com/foryou",
                    "https://www.tiktok.com",
                    "https://www.tiktok.com/@tiktok",
                    "https://www.tiktok.com/foryou",
                ]
                await session.tab.get(random.choice(try_urls))
            except Exception as e:
                self.logger.error(f"Session died during x-bogus generation: {e}")
                await self._mark_session_invalid(session)
                raise

    async def _ensure_signer_loaded(self, session):
        """Ensure window.byted_acrawler is available, self-healing if not.

        Fast path: the SDK is already present from the page load. If it is
        missing, inject the cached SDK source via eval rather than blindly
        reloading. Only when no cached SDK exists do we fall back to the
        legacy reload-retry loop.
        """
        try:
            await self._poll_for_condition(
                session.tab, "window.byted_acrawler !== undefined", timeout=10
            )
            await self._capture_signing_sdk(session)
            return
        except asyncio.TimeoutError:
            pass

        if await self._inject_signing_sdk(session):
            return

        await self._reload_until_signer(session)
        await self._capture_signing_sdk(session)

    async def generate_x_bogus(self, url: str, **kwargs):
        try:
            _, session = await self._get_valid_session_index(**kwargs)
        except Exception:
            _, session = self._get_session(**kwargs)

        await self._ensure_signer_loaded(session)

        try:
            result = await session.tab.evaluate(
                f'window.byted_acrawler.frontierSign("{url}")',
                await_promise=True,
            )
            return result
        except Exception as e:
            self.logger.error(f"Session died during x-bogus evaluation: {e}")
            await self._mark_session_invalid(session)
            raise

    async def sign_url(self, url: str, **kwargs):
        """Sign a url with X-Bogus and X-Gnarly parameters."""
        try:
            i, session = await self._get_valid_session_index(**kwargs)
        except Exception:
            i, session = self._get_session(**kwargs)

        sign_result = await self.generate_x_bogus(url, session_index=i)

        x_bogus = sign_result.get("X-Bogus")
        if x_bogus is None:
            raise Exception("Failed to generate X-Bogus")

        if "?" in url:
            url += "&"
        else:
            url += "?"
        url += f"X-Bogus={x_bogus}"

        x_gnarly = sign_result.get("X-Gnarly")
        if x_gnarly:
            url += f"&X-Gnarly={x_gnarly}"

        return url

    # ------------------------------------------------------------------
    # make_request
    # ------------------------------------------------------------------

    async def make_request(
        self,
        url: str,
        headers: dict = None,
        params: dict = None,
        retries: int = 3,
        exponential_backoff: bool = True,
        invalid_response_callback: Optional[callable] = lambda r: False,
        **kwargs,
    ):
        try:
            i, session = await self._get_valid_session_index(**kwargs)
        except Exception:
            i, session = self._get_session(**kwargs)

        if session.params is not None:
            params = {**session.params, **params}

        if headers is not None:
            headers = {**session.headers, **headers}
        else:
            headers = session.headers

        # get msToken
        if params.get("msToken") is None:
            if session.ms_token is not None:
                params["msToken"] = session.ms_token
            else:
                cookies = await self.get_session_cookies(session)
                ms_token = cookies.get("msToken")
                if ms_token is None:
                    self.logger.warning(
                        "Failed to get msToken from cookies, trying anyway (probably will fail)"
                    )
                params["msToken"] = ms_token

        encoded_params = f"{url}?{urlencode(params, safe='=', quote_via=quote)}"
        signed_url = await self.sign_url(encoded_params, session_index=i)

        retry_count = 0
        while retry_count < retries:
            retry_count += 1
            try:
                result = await self.run_fetch_script(
                    signed_url, headers=headers, session_index=i
                )

                if result is None:
                    raise Exception("TikTokApi.run_fetch_script returned None")

                if result == "":
                    raise EmptyResponseException(
                        result,
                        "TikTok returned an empty response. "
                        "They are detecting you're a bot, consider using a proxy",
                    )

                try:
                    data = json.loads(result)
                    status_code = max(data.get('statusCode', 0), data.get('status_code', 0))
                    if status_code != 0:
                        self.logger.error(f"Got an unexpected status code: {data}")
                    if status_code == 0 and invalid_response_callback(data):
                        raise Exception("Response failed validation")
                    return data
                except json.decoder.JSONDecodeError:
                    if retry_count == retries:
                        self.logger.error(f"Failed to decode json response: {result}")
                        raise InvalidJSONException()

                    self.logger.info(
                        f"Failed a request, retrying ({retry_count}/{retries})"
                    )
                    if exponential_backoff:
                        await asyncio.sleep(2**retry_count)
                    else:
                        await asyncio.sleep(1)
            except Exception as e:
                # Check if this is a session-level failure (tab died, etc.)
                self.logger.error(f"Error during request: {e}")
                await self._mark_session_invalid(session)

                if retry_count < retries:
                    self.logger.info(
                        f"Retrying with a new session ({retry_count}/{retries})"
                    )
                    try:
                        i, session = await self._get_valid_session_index(**kwargs)
                    except Exception as session_error:
                        self.logger.error(
                            f"Failed to get valid session: {session_error}"
                        )
                        raise
                else:
                    raise

    # ------------------------------------------------------------------
    # Content / stats
    # ------------------------------------------------------------------

    async def get_session_content(self, url: str, **kwargs):
        try:
            _, session = await self._get_valid_session_index(**kwargs)
        except Exception:
            _, session = self._get_session(**kwargs)

        try:
            return await session.tab.get_content()
        except Exception as e:
            self.logger.error(f"Session died during get_session_content: {e}")
            await self._mark_session_invalid(session)
            raise

    def get_resource_stats(self) -> dict:
        valid_sessions = sum(1 for s in self.sessions if s.is_valid)
        invalid_sessions = len(self.sessions) - valid_sessions
        return {
            "total_sessions": len(self.sessions),
            "valid_sessions": valid_sessions,
            "invalid_sessions": invalid_sessions,
            "has_browser": self.browser is not None,
            "cleanup_called": self._cleanup_called,
            "auto_cleanup_enabled": self._auto_cleanup_dead_sessions,
            "recovery_enabled": self._session_recovery_enabled,
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close_sessions()
