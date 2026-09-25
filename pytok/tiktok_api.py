"""API client that issues TikTok's own signed requests from the browser page.

TikTok's webmssdk wraps the page's fetch and XHR and signs every API request that goes
through them (X-Bogus, X-Gnarly, msToken). So a plain fetch from the page's main world
goes out signed exactly like the webapp's own requests, and nothing here signs anything.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import random
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from .exceptions import (
    EmptyResponseException,
    InvalidJSONException,
    NoTemplateException,
    ResponseValidationException,
)


@dataclasses.dataclass
class TikTokSession:
    """A TikTok session backed by a Playwright page."""

    page: Any
    proxy: str = None
    params: dict = None
    headers: dict = None
    base_url: str = "https://www.tiktok.com"
    is_valid: bool = True


class TikTokApiClient:
    """TikTok API client sharing PyTok's browser page.

    Signs and fetches in the same page PyTok uses for network capture and DOM scraping.
    PyTok owns the page and the browser; this client never closes either.
    """

    # webmssdk.js defines window.byted_acrawler and wraps fetch/XHR to sign requests.
    # The URL is normally discovered from the live DOM so the version stays current;
    # this hardcoded version is a stale-prone last resort only.
    _SIGNING_SDK_URL_FALLBACK = (
        "https://sf16-website-login.neutral.ttwstatic.com/obj/"
        "tiktok_web_login_static/webmssdk/1.0.0.374/webmssdk.js"
    )

    # Params the SDK adds to every request it signs. Never replayed from a captured
    # template, and ignored when matching a request on the wire to one we issued.
    _SIGNING_PARAMS = frozenset({
        "msToken", "X-Bogus", "X-Gnarly", "X-Dynosaur",
    })

    # Templates rot: captured params include moment-bound values (time_of_day,
    # day_of_week, window state, ...) that TikTok cross-checks. Replaying a
    # template past this age gets playAddr URLs in the response poisoned (they
    # 403 on download) even though the JSON response itself still succeeds.
    # Treat an aged template as absent so the lazy scraping route re-captures
    # a fresh one.
    TEMPLATE_TTL_SECONDS = 15 * 60

    def __init__(self, logging_level: Optional[int] = None, logger_name: str = None):
        self.sessions = []
        self._cleanup_called = False
        self.context = None
        self._shared_page = None
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
        # URLs of fetches we issued ourselves, so PyTok's capture handler never
        # recycles our own (template-derived) requests back into the template cache.
        self._inflight_fetch_urls = set()

        if logger_name is None:
            logger_name = "TikTokApiClient"
        self._create_logger(logger_name, logging_level)

    def _create_logger(self, name: str, level: Optional[int] = None):
        """Create a logger for the class. level=None leaves the logger's level alone."""
        self.logger: logging.Logger = logging.getLogger(name)
        if level is not None:
            self.logger.setLevel(level)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

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
        template for that endpoint type (freshest observation wins).

        Called by PyTok's capture handler for every API request the webapp's
        own JS issues; our own fetches are excluded via _inflight_fetch_urls.
        """
        key = self._endpoint_key(url)
        is_new = key not in self._api_param_cache
        self._api_param_cache[key] = (dict(params), time.monotonic())
        if is_new:
            self.logger.info(f"Captured param template for endpoint '{key}'")

    def get_cached_api_params(self, url: str) -> Optional[dict]:
        key = self._endpoint_key(url)
        entry = self._api_param_cache.get(key)
        if entry is None:
            return None
        params, captured_at = entry
        if time.monotonic() - captured_at > self.TEMPLATE_TTL_SECONDS:
            self.logger.info(
                f"Param template for endpoint '{key}' exceeded its "
                f"{self.TEMPLATE_TTL_SECONDS}s TTL; treating as absent so the "
                "scraping route re-captures a fresh one"
            )
            del self._api_param_cache[key]
            return None
        return params

    def _unsigned_params(self, url: str) -> frozenset:
        return frozenset(
            (k, v) for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True)
            if k not in self._SIGNING_PARAMS
        )

    def is_self_issued(self, url: str) -> bool:
        """True if this request URL is one of our own in-flight fetches.

        The URL on the wire is ours plus whatever the SDK appended to sign it, so
        compare on the endpoint and the decoded params the SDK does not touch.
        """
        if not self._inflight_fetch_urls:
            return False
        key = self._endpoint_key(url)
        params = self._unsigned_params(url)
        return any(
            self._endpoint_key(inflight) == key and self._unsigned_params(inflight) == params
            for inflight in self._inflight_fetch_urls
        )

    def invalidate_cached_api_params(self, url: str):
        """Drop a (presumed stale/burned) endpoint template so the next request
        for this type lazily refills it via the frontend scraping route."""
        key = self._endpoint_key(url)
        if self._api_param_cache.pop(key, None) is not None:
            self.logger.info(f"Invalidated param template for endpoint '{key}'")

    def clear_api_param_cache(self):
        self._api_param_cache = {}

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def _is_session_valid(self, session) -> bool:
        if not session.is_valid:
            return False
        try:
            if session.page.is_closed():
                session.is_valid = False
                return False
            await session.page.evaluate("1")
            return True
        except Exception as e:
            self.logger.warning(f"Session validation failed: {e}")
            session.is_valid = False
            return False

    async def _get_valid_session_index(self, **kwargs):
        """Get a valid session.

        Args:
            session_index (int, optional): Specific session index to use.

        Returns:
            tuple: (index, session)

        Raises:
            Exception: If the shared page is gone.
        """
        if kwargs.get("session_index") is not None:
            i = kwargs["session_index"]
            if i < len(self.sessions) and await self._is_session_valid(self.sessions[i]):
                return i, self.sessions[i]
            self.logger.warning(f"Requested session {i} is invalid")
        else:
            valid = [(i, s) for i, s in enumerate(self.sessions) if await self._is_session_valid(s)]
            if valid:
                return random.choice(valid)
        raise Exception(
            "No valid sessions available: the browser page has closed or stopped "
            "responding. Rebuild the PyTok session."
        )

    async def create_sessions(
        self,
        context,
        existing_page,
        headers: dict | None = None,
        starting_url: str = "https://www.tiktok.com",
        **kwargs,
    ):
        """Bind the client to PyTok's shared page as a single session.

        Args:
            context: The Playwright BrowserContext the page belongs to (for cookies).
            existing_page: PyTok's page to share.
            headers: Request headers PyTok captured from the page's initial
                navigation; used by the httpx/requests download paths.
            starting_url: Base URL for the session.
        """
        self.context = context
        self._shared_page = existing_page
        self._shared_headers = dict(headers) if headers else {}
        self._shared_base_url = starting_url
        self._cleanup_called = False
        self.sessions = [TikTokSession(
            page=existing_page,
            headers=self._shared_headers,
            base_url=starting_url,
        )]

    async def close_sessions(self):
        """Drop the session reference. Does NOT close the shared page or the
        browser — PyTok owns both and tears them down itself."""
        self.sessions.clear()
        self._shared_page = None
        self._cleanup_called = True
        self.logger.debug("Session reference cleared")

    async def refresh_session_params(self):
        """Mark the shared session usable again after PyTok re-navigated its page."""
        if not self.sessions and self._shared_page is not None:
            await self.create_sessions(self.context, self._shared_page,
                                       self._shared_headers, self._shared_base_url)
        for session in self.sessions:
            session.is_valid = True

    # ------------------------------------------------------------------
    # JS in the page's main world
    # ------------------------------------------------------------------

    @staticmethod
    async def evaluate_main_world(page, expression: str):
        """Evaluate in the page's own JS world rather than Playwright's isolated one.

        Needed for anything that touches the webapp's globals: the signer lives there,
        and only a fetch issued there goes through the SDK's wrapper and gets signed.
        Requires the browser to have been launched with main_world_eval.
        """
        return await page.evaluate("mw:" + expression)

    async def run_fetch_script(self, url: str, **kwargs):
        try:
            _, session = await self._get_valid_session_index(**kwargs)
        except Exception:
            _, session = self._get_session(**kwargs)

        js = (
            "(async () => {"
            f"  const resp = await fetch({json.dumps(url)}, {{ credentials: 'include' }});"
            "  return await resp.text();"
            "})()"
        )
        # Registered so PyTok's capture handler can tell this self-issued fetch
        # apart from the webapp's own requests and never recycles it into the
        # per-endpoint template cache.
        self._inflight_fetch_urls.add(url)
        try:
            return await self.evaluate_main_world(session.page, js)
        except Exception as e:
            self.logger.error(f"Session failed during fetch: {e}")
            session.is_valid = False
            raise
        finally:
            self._inflight_fetch_urls.discard(url)

    # ------------------------------------------------------------------
    # Cookies
    # ------------------------------------------------------------------

    async def get_session_cookies(self, session=None):
        cookies = await self.context.cookies()
        return {cookie["name"]: cookie["value"] for cookie in cookies}

    # ------------------------------------------------------------------
    # Signer
    # ------------------------------------------------------------------

    async def _signer_present(self, session) -> bool:
        return bool(await self.evaluate_main_world(
            session.page, "typeof window.byted_acrawler !== 'undefined'"
        ))

    async def _poll_for_signer(self, session, timeout):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if await self._signer_present(session):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _capture_signing_sdk(self, session):
        """Fetch and cache webmssdk.js from a healthy session (once).

        Captured from TikTok itself rather than vendored, so the signer stays
        version-matched. Reused to re-inject the signer into sessions where the
        SDK failed to load. Best-effort: failures are logged and ignored.
        """
        if self._signing_sdk_src is not None:
            return
        try:
            sdk_url = await session.page.evaluate(
                "(document.querySelector('script[src*=\"webmssdk/\"]') || {}).src || null"
            ) or self._SIGNING_SDK_URL_FALLBACK
            src = await session.page.evaluate(
                f"fetch({json.dumps(sdk_url)}).then(r => r.text())"
            )
            if src and len(src) > 1000:
                self._signing_sdk_src = src
                self.logger.debug(f"Captured signing SDK ({len(src)} bytes) from {sdk_url}")
        except Exception as e:
            self.logger.debug(f"Failed to capture signing SDK: {e}")

    async def _inject_signing_sdk(self, session) -> bool:
        """Re-inject the cached signer via indirect eval in the main world.

        webmssdk only defines window.byted_acrawler when executed in global
        scope via indirect eval; a <script> tag or document-start injection
        does not work. Returns True if byted_acrawler is available afterwards.
        """
        if self._signing_sdk_src is None:
            return False
        try:
            await self.evaluate_main_world(
                session.page,
                f"(window.__pytok_sdk__ = {json.dumps(self._signing_sdk_src)}, void 0)",
            )
            await self.evaluate_main_world(session.page, "((0,eval)(window.__pytok_sdk__), void 0)")
            present = await self._signer_present(session)
            if present:
                self.logger.info("Re-injected signing SDK into session via eval")
            return present
        except Exception as e:
            self.logger.debug(f"Failed to inject signing SDK: {e}")
            return False

    async def _reload_until_signer(self, session):
        """Last resort: reload TikTok pages until byted_acrawler appears."""
        max_attempts = 5
        try_urls = [
            "https://www.tiktok.com/foryou",
            "https://www.tiktok.com",
            "https://www.tiktok.com/@tiktok",
        ]
        for attempt in range(1, max_attempts + 1):
            if await self._poll_for_signer(session, timeout=random.uniform(5, 20)):
                return
            if attempt == max_attempts:
                raise asyncio.TimeoutError(
                    f"Signer did not load after {max_attempts} page loads, consider using a proxy"
                )
            try:
                await session.page.goto(random.choice(try_urls), wait_until="domcontentloaded")
            except Exception as e:
                self.logger.error(f"Session died while waiting for the signer: {e}")
                session.is_valid = False
                raise

    async def _ensure_signer_loaded(self, session):
        """Make sure the SDK is in the page, so our fetch gets signed.

        Fast path: the SDK is already present from the page load. If it is
        missing, inject the cached SDK source rather than blindly reloading.
        Only when no cached SDK exists do we fall back to reloading pages.
        """
        if await self._poll_for_signer(session, timeout=10):
            await self._capture_signing_sdk(session)
            return
        if await self._inject_signing_sdk(session):
            return
        await self._reload_until_signer(session)
        await self._capture_signing_sdk(session)

    # ------------------------------------------------------------------
    # make_request
    # ------------------------------------------------------------------

    async def make_request(
        self,
        url: str,
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
        base = {k: v for k, v in template.items() if k not in self._SIGNING_PARAMS}
        params = {**base, **(params or {})}
        request_url = f"{url}?{urlencode(params, safe='=', quote_via=quote)}"

        await self._ensure_signer_loaded(session)

        retry_count = 0
        while retry_count < retries:
            retry_count += 1
            try:
                result = await self.run_fetch_script(request_url, session_index=i)

                if result is None:
                    raise Exception("run_fetch_script returned None")

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
                # NOT a dead session: tearing the browser down makes bot detection
                # more likely, not less. Retry on the same session; after exhausting
                # retries, drop this endpoint's template (it's evidently stale/burned)
                # and propagate so the caller falls back to scraping — which both
                # serves the current request and re-captures a fresh template.
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
                # Session-level failure (page closed, evaluation failed, ...).
                self.logger.error(f"Error during request: {e}")
                session.is_valid = False
                if retry_count < retries:
                    self.logger.info(f"Retrying request ({retry_count}/{retries})")
                    try:
                        i, session = await self._get_valid_session_index(**kwargs)
                    except Exception as session_error:
                        self.logger.error(f"Failed to get valid session: {session_error}")
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
        return await session.page.content()

    def get_resource_stats(self) -> dict:
        valid_sessions = sum(1 for s in self.sessions if s.is_valid)
        return {
            "total_sessions": len(self.sessions),
            "valid_sessions": valid_sessions,
            "invalid_sessions": len(self.sessions) - valid_sessions,
            "has_context": self.context is not None,
            "cleanup_called": self._cleanup_called,
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close_sessions()
