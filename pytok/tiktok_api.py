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
from typing import Any, Optional
from urllib.parse import urlencode, quote, urlparse

from zendriver import cdp
from zendriver.core.connection import ProtocolException

from .exceptions import (
    InvalidJSONException,
    EmptyResponseException,
    ResponseValidationException,
    NoTemplateException,
)


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

    # Params never replayed from a cached per-endpoint template: signatures and
    # tokens are stale the moment they're captured (they're re-generated fresh
    # per request instead). Everything else in a captured request — including
    # endpoint-specific keys like cursor/secUid — is kept as the template value
    # and simply overridden by the caller's own per-request params.
    _TEMPLATE_EXCLUDED_PARAMS = frozenset({
        "msToken", "X-Bogus", "X-Gnarly", "X-Dynosaur",
    })

    def __init__(self, logging_level: int = logging.WARN, logger_name: str = None):
        self.sessions = []
        self._session_recovery_enabled = True
        self._session_creation_lock = asyncio.Lock()
        self._cleanup_called = False
        self._owns_browser = False
        self.browser = None
        # The single browser tab shared with PyTok. This client signs and fetches
        # in the same foreground tab that PyTok uses for CDP network capture and
        # DOM scraping, so there are no background session tabs to keep alive.
        # PyTok owns this tab's lifecycle; we must never close it.
        self._shared_tab = None
        self._shared_headers = None
        self._shared_base_url = "https://www.tiktok.com"
        # Cached webmssdk.js source, captured from a healthy session and
        # re-injected to self-heal sessions where the signer failed to load.
        self._signing_sdk_src = None
        # Per-endpoint param templates, lazily captured off the wire by PyTok
        # from the webapp's own API requests (keyed by URL path, e.g.
        # 'api/post/item_list'). The first request for an endpoint type must go
        # through the frontend scraping route, which fires the webapp's own
        # request and fills the cache; subsequent requests reuse the template.
        # TikTok binds response trust (and the CDN signatures of returned
        # playAddr URLs) to the requesting fingerprint, and each endpoint has
        # its own param shape — replaying another endpoint's params (or made-up
        # ones) invites bot detection / empty responses.
        self._api_param_cache = {}
        # Freshest msToken seen on any captured webapp request. TikTok rotates
        # the token constantly; a stale one is rejected on logged-in sessions.
        self._latest_ms_token = None
        # Signed URLs of fetches we issued ourselves, so PyTok's capture
        # handler never recycles our own (template-derived) requests back into
        # the template cache.
        self._inflight_fetch_urls = set()

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
    # Per-endpoint param template cache
    # ------------------------------------------------------------------

    @staticmethod
    def _endpoint_key(url: str) -> str:
        """Normalize an API URL to its cache key, e.g. 'api/post/item_list'."""
        return urlparse(url).path.strip('/')

    def cache_api_params(self, url: str, params: dict):
        """Store the query params of a webapp-issued API request as the
        template for that endpoint type (freshest observation wins), and lift
        its msToken as the newest known token.

        Called by PyTok's CDP capture handler for every API request the
        webapp's own JS issues; our own fetches are excluded via
        _inflight_fetch_urls.
        """
        key = self._endpoint_key(url)
        is_new = key not in self._api_param_cache
        self._api_param_cache[key] = dict(params)
        ms_token = params.get("msToken")
        if ms_token:
            self._latest_ms_token = ms_token
        if is_new:
            self.logger.info(f"Captured param template for endpoint '{key}'")

    def get_cached_api_params(self, url: str) -> Optional[dict]:
        return self._api_param_cache.get(self._endpoint_key(url))

    def is_self_issued(self, url: str) -> bool:
        """True if this request URL is one of our own in-flight fetches.

        Exact match against the registered signed URLs, plus a tolerant
        fallback (same endpoint + same X-Bogus signature) in case the browser
        re-normalizes the URL string on the wire.
        """
        if url in self._inflight_fetch_urls:
            return True
        key = self._endpoint_key(url)
        for inflight in self._inflight_fetch_urls:
            if self._endpoint_key(inflight) != key:
                continue
            marker = inflight.rsplit("X-Bogus=", 1)
            if len(marker) == 2 and f"X-Bogus={marker[1].split('&')[0]}" in url:
                return True
        return False

    def invalidate_cached_api_params(self, url: str):
        """Drop a (presumed stale/burned) endpoint template so the next request
        for this type lazily refills it via the frontend scraping route."""
        key = self._endpoint_key(url)
        if self._api_param_cache.pop(key, None) is not None:
            self.logger.info(f"Invalidated param template for endpoint '{key}'")

    def clear_api_param_cache(self):
        self._api_param_cache = {}
        self._latest_ms_token = None

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
            # A tab can be "open" with a readable URL yet have no JS execution
            # context (Chrome froze/discarded a backgrounded tab). That only
            # shows up as "Cannot find default execution context" mid-fetch, so
            # probe it here. If the context is gone, try to reactivate the tab
            # before writing the session off.
            try:
                await session.tab.send(
                    cdp.runtime.evaluate(expression="1", return_by_value=True)
                )
            except Exception:
                await session.tab.send(
                    cdp.emulation.set_focus_emulation_enabled(True)
                )
                await session.tab.send(cdp.page.set_web_lifecycle_state("active"))
                await session.tab.send(
                    cdp.runtime.evaluate(expression="1", return_by_value=True)
                )
            return True
        except Exception as e:
            self.logger.warning(f"Session validation failed: {e}")
            session.is_valid = False
            return False

    async def _mark_session_invalid(self, session):
        # The session lives on PyTok's shared main tab, which we don't own — so
        # we never close it and never drop it from the pool (there's nothing to
        # recreate). Just flag it; _recover_sessions reactivates the tab.
        session.is_valid = False

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

    async def _build_shared_session(self):
        """Wrap the shared main tab in a single TikTokSession.

        Reads the current msToken from the browser cookie jar and derives the
        session params from the live tab. Headers come from PyTok, which
        captured them off the main tab's initial navigation.
        """
        session = TikTokSession(
            tab=self._shared_tab,
            ms_token=None,
            headers=self._shared_headers,
            base_url=self._shared_base_url,
            is_valid=True,
        )
        cookies_dict = await self.get_session_cookies(session)
        session.ms_token = cookies_dict.get("msToken")
        if session.ms_token is None:
            self.logger.info(
                "Failed to get msToken from cookies; requests may fail. "
                "Consider passing an ms_token."
            )
        return session

    async def create_sessions(
        self,
        zendriver_browser,
        existing_tab,
        headers: dict | None = None,
        starting_url: str = "https://www.tiktok.com",
        enable_session_recovery: bool = True,
        **kwargs,
    ):
        """Bind the client to PyTok's shared main tab as a single session.

        The client signs and fetches in the same foreground tab PyTok uses for
        network capture and scraping. There are no background tabs, so nothing
        needs keep-alive treatment against Chrome freezing.

        Args:
            zendriver_browser: The zendriver Browser instance (required).
            existing_tab: PyTok's main page tab to share (required).
            headers: Request headers PyTok captured from the main tab's initial
                navigation; used for signed fetches and the httpx/requests paths.
            starting_url: Base URL for the session.
            enable_session_recovery: Enable reactivation of a stalled tab.
        """
        self._session_recovery_enabled = enable_session_recovery
        self.browser = zendriver_browser
        self._shared_tab = existing_tab
        self._shared_headers = dict(headers) if headers else {}
        self._shared_base_url = starting_url
        self._cleanup_called = False

        self.sessions = [await self._build_shared_session()]

    async def _recover_sessions(self):
        """Reactivate the shared tab rather than spawning a new one.

        The single session lives on PyTok's main tab, which we don't own and
        can't recreate. If it stalled (lost its JS execution context), pin it
        active again and rebuild the session wrapper around it.
        """
        async with self._session_creation_lock:
            if self._shared_tab is None:
                return
            self.logger.info("Reactivating shared session tab...")
            try:
                await self._shared_tab.send(
                    cdp.emulation.set_focus_emulation_enabled(True)
                )
                await self._shared_tab.send(
                    cdp.page.set_web_lifecycle_state("active")
                )
                await self._shared_tab.send(
                    cdp.runtime.evaluate(expression="1", return_by_value=True)
                )
                self.sessions = [await self._build_shared_session()]
                self.logger.info("Shared session reactivated")
            except Exception as e:
                self.logger.error(f"Failed to reactivate shared session: {e}")

    # ------------------------------------------------------------------
    # Session cleanup
    # ------------------------------------------------------------------

    async def close_sessions(self):
        """Drop the session reference. Does NOT close the shared main tab or the
        browser — PyTok owns both and tears them down itself."""
        self.sessions.clear()
        self._shared_tab = None
        self._cleanup_called = True
        self.logger.debug("Session reference cleared")

    async def refresh_session_params(self):
        """Re-derive params and msToken for the shared session in place.

        Called after PyTok re-navigates the main tab to refresh cookies/tokens.
        No tab is closed or recreated.
        """
        if not self.sessions:
            if self._shared_tab is not None:
                self.sessions = [await self._build_shared_session()]
            return
        for session in self.sessions:
            cookies = await self.get_session_cookies(session)
            ms_token = cookies.get("msToken")
            if ms_token:
                session.ms_token = ms_token
            session.is_valid = True

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

        # Register the URL so PyTok's CDP capture handler can tell this
        # self-issued fetch apart from the webapp's own requests and never
        # recycles it into the per-endpoint template cache.
        self._inflight_fetch_urls.add(url)
        try:
            result = await self._evaluate(session.tab, js_script, await_promise=True)
            return result
        except Exception as e:
            self.logger.error(f"Session failed during fetch: {e}")
            await self._mark_session_invalid(session)
            raise
        finally:
            self._inflight_fetch_urls.discard(url)

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

        # Stale X-Dynosaur values are stripped from cached templates; attach a
        # fresh one when the signer can produce it.
        x_dynosaur = sign_result.get("X-Dynosaur")
        if x_dynosaur:
            url += f"&X-Dynosaur={x_dynosaur}"

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

        # Lazily-filled per-endpoint template: the first request for an
        # endpoint type must go through the frontend scraping route, which
        # fires the webapp's own request and fills the cache. No template ->
        # tell the caller to scrape (NoTemplateException is an
        # ApiFailedException, so every existing fallback handles it).
        template = self.get_cached_api_params(url)
        if template is None:
            raise NoTemplateException(
                f"No param template captured yet for '{self._endpoint_key(url)}' "
                "— falling back to scraping to fill the cache"
            )
        base = {
            k: v for k, v in template.items()
            if k not in self._TEMPLATE_EXCLUDED_PARAMS
        }
        params = {**base, **params}

        if headers is not None:
            headers = {**session.headers, **headers}
        else:
            headers = session.headers

        # msToken: TikTok rotates it constantly and (on logged-in sessions)
        # rejects stale ones, so resolve it fresh per request — the newest of
        # the live cookie and the token seen on the latest captured webapp
        # request — rather than a value cached at session build.
        if params.get("msToken") is None:
            cookies = await self.get_session_cookies(session)
            ms_token = cookies.get("msToken") or self._latest_ms_token or session.ms_token
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
                        # Well-formed response that lacks the fields we need. This is a
                        # request-level failure (bot detection / degraded API response),
                        # NOT a dead session — raise a request-level exception so we keep
                        # the session, retry, then let the caller fall back to scraping,
                        # instead of invalidating the session and rebuilding the browser.
                        raise ResponseValidationException(result, "Response failed validation")
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
            except (EmptyResponseException, InvalidJSONException) as e:
                # Request-level failure (bot detection / rate limiting / bad JSON),
                # NOT a dead session. The tab, cookies and msToken are still good;
                # tearing the session down and recovering a fresh one makes bot
                # detection *more* likely (a new msToken looks less trustworthy),
                # and it empties the pool so the next handle can't resume the API
                # path without a full recovery. So keep the session and retry on
                # it; after exhausting retries, drop this endpoint's template
                # (it's evidently stale/burned) and propagate the exception so
                # the caller falls back to scraping — which both serves the
                # current request and re-captures a fresh template off the wire.
                if retry_count < retries:
                    self.logger.info(
                        f"Empty/invalid response ({type(e).__name__}), "
                        f"retrying on same session ({retry_count}/{retries})"
                    )
                    if exponential_backoff:
                        await asyncio.sleep(2 ** retry_count)
                    else:
                        await asyncio.sleep(1)
                else:
                    self.invalidate_cached_api_params(url)
                    raise
            except Exception as e:
                # Session-level failure (tab died, protocol error, etc.): the
                # session is genuinely unusable, so invalidate it and recover.
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
            "recovery_enabled": self._session_recovery_enabled,
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close_sessions()
