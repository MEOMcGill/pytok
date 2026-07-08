import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import time
from typing import Optional

from urllib.parse import parse_qs, urlparse

import zendriver as zd
from zendriver import cdp
import random

from ._cdp_patches import apply_cdp_patches
from .tiktok_api import ZendriverTikTokApi

# Patch zendriver's CDP bindings for Chrome 149+ before any browser is started.
apply_cdp_patches()

from .api.sound import Sound
from .api.user import User
from .api.search import Search
from .api.hashtag import Hashtag
from .api.video import Video
from .api.trending import Trending

from .exceptions import *
from .utils import LOGGER_NAME

os.environ["no_proxy"] = "127.0.0.1,localhost"

BASE_URL = "https://m.tiktok.com/"
DESKTOP_BASE_URL = "https://www.tiktok.com/"


class PyTok:
    _is_context_manager = False
    logger = logging.getLogger(LOGGER_NAME)

    # Default browser args for stealth
    _DEFAULT_BROWSER_ARGS = [
        '--disable-blink-features=AutomationControlled',
        '--disable-infobars',
        '--disable-dev-shm-usage',
        '--no-first-run',
        '--disable-background-networking',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
        '--mute-audio',
    ]

    # JavaScript to override Page Visibility API and focus detection.
    # TikTok checks these to detect backgrounded/unfocused browser tabs.
    _VISIBILITY_OVERRIDE_JS = """
    // Override document.hidden to always return false
    Object.defineProperty(document, 'hidden', {
        get: function() { return false; },
        configurable: true
    });

    // Override document.visibilityState to always return 'visible'
    Object.defineProperty(document, 'visibilityState', {
        get: function() { return 'visible'; },
        configurable: true
    });

    // Override document.hasFocus to always return true
    Document.prototype.hasFocus = function() { return true; };

    // Suppress visibilitychange events so TikTok never sees a state transition
    document.addEventListener('visibilitychange', function(e) {
        e.stopImmediatePropagation();
    }, true);

    // Override the onvisibilitychange handler setter to be a no-op
    Object.defineProperty(document, 'onvisibilitychange', {
        get: function() { return null; },
        set: function(v) {},
        configurable: true
    });
    """

    def __init__(
            self,
            logging_level: int = logging.WARNING,
            request_delay: Optional[int] = 0,
            headless: Optional[bool] = False,
            manual_captcha_solves: Optional[bool] = False,
            log_captcha_solves: Optional[bool] = False,
            num_sessions: int = 1,
            user_data_dir: Optional[str] = None,
            browser_args: Optional[list] = None,
            page_load_timeout: Optional[int] = 30,
            account=None,
            accounts_pool=None,
            release_on_shutdown: bool = True,
            force_relogin: bool = False,
            startup_lock: Optional[asyncio.Lock] = None,
    ):
        """The PyTok class. Used to interact with TikTok.

        ##### Parameters
        * logging_level: The logging level you want the program to run at, optional
            These are the standard python logging module's levels.

        * request_delay: The amount of time in seconds to wait before making a request, optional
            This is used to throttle your own requests as you may end up making too
            many requests to TikTok for your IP.

        * num_sessions: Number of browser sessions to create (used by TikTok-Api), optional

        * user_data_dir: Path to Chrome user data directory for profile persistence, optional
            If not provided, uses a fresh profile each session. Set to your Chrome
            profile path (e.g., ~/.config/google-chrome) to reuse cookies/history.
            Note: Don't use a profile that's open in another Chrome instance.

        * browser_args: Additional Chrome command-line arguments, optional
            Merged with default stealth args. Pass empty list [] to disable defaults.

        * page_load_timeout: Seconds to wait for the initial tiktok.com navigation to
            reach readyState 'complete', optional. Cold Chrome starts against a large
            persistent profile plus a slow TikTok homepage can exceed the old 10s; raise
            this if setup keeps timing out.

        * account: An accounts.Account to run this session as, optional. When set,
            its persistent Chrome profile dir is used (unless user_data_dir is given
            explicitly) and __aenter__ verifies the profile is logged into that
            account (repairing from the cookie backup or a login flow if not).

        * accounts_pool: The accounts.AccountsPool the account came from, optional.
            When set, cookies/identity are persisted back to it and the account is
            released on shutdown. Usually you obtain both via PyTok.from_pool(...).

        * release_on_shutdown: If True (default), shutdown releases the account
            back to the pool (in_use=false). A WorkerPool sets this False so it
            can own the account across many tasks and rebuild a crashed session
            on the same account without a release/re-acquire race; the worker
            releases the account itself when it is finally done with it.

        * startup_lock: An asyncio.Lock shared across PyTok instances that launch
            browsers concurrently (e.g. a WorkerPool's workers). Held only around
            the browser-launch phase (zendriver start + session bind), where
            zendriver's free_port() is TOCTOU: two Chromes starting at the same
            instant can pick the same debug port. Serializing that phase makes
            startup deterministic; it is released before account verification so
            a slow login/captcha on one worker doesn't block the others'
            startup. None (default) = no serialization (standalone use).
            (Historical note: most concurrent-startup failures — e.g. "No
            sessions created" — were actually caused by API classes sharing a
            class-level `parent`, so every new PyTok hijacked all workers'
            objects; parent is now bound per instance via the api factories.)
        """
        # assert headless is False, "Running in headless currently does not work reliably."

        self._account = account
        self._accounts_pool = accounts_pool
        self._release_on_shutdown = release_on_shutdown
        # Shared across concurrent PyTok launches to serialize the racy
        # browser-startup phase (see the startup_lock docstring above).
        self._startup_lock = startup_lock
        # When True, verification clears the profile's session first and forces a
        # fresh credentialed login — used to recover an account whose cookies look
        # valid but whose session TikTok has invalidated server-side.
        self._force_relogin = force_relogin
        # Set True only once the live logged-in uid is confirmed to match the
        # account. Gates cookie snapshots so a stale/unverified session can never
        # overwrite a good cookie backup.
        self._identity_confirmed = False
        # An attached account supplies its persistent profile dir unless the caller
        # overrode user_data_dir explicitly.
        if account is not None and user_data_dir is None:
            user_data_dir = account.profile_dir
            if user_data_dir:
                os.makedirs(user_data_dir, exist_ok=True)

        self._headless = headless
        self._request_delay = request_delay
        self._manual_captcha_solves = manual_captcha_solves
        self._log_captcha_solves = log_captcha_solves
        self._num_sessions = num_sessions
        self._user_data_dir = user_data_dir
        self._page_load_timeout = page_load_timeout
        # Merge browser args: use defaults unless explicitly disabled with empty list
        if browser_args is None:
            self._browser_args = self._DEFAULT_BROWSER_ARGS.copy()
        elif browser_args == []:
            self._browser_args = []
        else:
            self._browser_args = self._DEFAULT_BROWSER_ARGS + browser_args

        self.logger.setLevel(logging_level)

        self.request_cache = {}

        # Create zendriver-based TikTokApi instance for API requests
        self.tiktok_api = ZendriverTikTokApi(
            logging_level=logging_level
        )

    # ------------------------------------------------------------------
    # API object factories
    #
    # Each factory binds the created object to THIS PyTok instance via an
    # instance-level `parent`. These used to be class aliases (`user = User`)
    # with `User.parent` stamped globally in __init__ — which meant every
    # PyTok constructed in the process hijacked `parent` for all existing
    # API objects. With N concurrent workers, worker A's objects would route
    # requests to worker B's (possibly half-built) browser, causing races
    # like "No sessions created" at startup. Instance binding removes that
    # shared state entirely.
    # ------------------------------------------------------------------

    def user(self, *args, **kwargs) -> User:
        """Create a User bound to this PyTok instance."""
        return User(*args, parent=self, **kwargs)

    def search(self, *args, **kwargs) -> Search:
        """Create a Search bound to this PyTok instance."""
        return Search(*args, parent=self, **kwargs)

    def sound(self, *args, **kwargs) -> Sound:
        """Create a Sound bound to this PyTok instance."""
        return Sound(*args, parent=self, **kwargs)

    def hashtag(self, *args, **kwargs) -> Hashtag:
        """Create a Hashtag bound to this PyTok instance."""
        return Hashtag(*args, parent=self, **kwargs)

    def video(self, *args, **kwargs) -> Video:
        """Create a Video bound to this PyTok instance."""
        return Video(*args, parent=self, **kwargs)

    def trending(self, *args, **kwargs) -> Trending:
        """Create a Trending bound to this PyTok instance."""
        return Trending(*args, parent=self, **kwargs)

    # URL patterns we care about - TikTok API and video media
    _TRACKED_URL_PATTERNS = [
        '/api/',           # TikTok API endpoints (comments, related videos, etc.)
        'video/tos',       # TikTok video CDN paths
        'v16-webapp',      # TikTok video CDN paths
        'v19-webapp',      # TikTok video CDN paths
    ]

    def _should_track_url(self, url: str) -> bool:
        """Check if URL matches patterns we want to track."""
        return any(pattern in url for pattern in self._TRACKED_URL_PATTERNS)

    def _on_response(self, event: cdp.network.ResponseReceived, connection=None):
        """Handle network response events from CDP."""
        if not isinstance(event, cdp.network.ResponseReceived):
            return
        url = event.response.url
        # Early filter - only track URLs we care about
        if not self._should_track_url(url):
            return
        request_id = event.request_id
        self._pending_requests[request_id] = {
            'url': url,
            'ready': False,
            'response': event.response
        }

    def _on_loading_finished(self, event: cdp.network.LoadingFinished, connection=None):
        """Mark request as ready for body fetch - no async work in callbacks."""
        if not isinstance(event, cdp.network.LoadingFinished):
            return
        request_id = event.request_id
        if request_id not in self._pending_requests:
            return
        self._pending_requests[request_id]['ready'] = True

    def _on_request_will_be_sent(self, event: cdp.network.RequestWillBeSent, connection=None):
        """Capture the browser's real request headers and per-endpoint API params.

        Headers: taken from the first outgoing request (user-agent, sec-ch-ua,
        accept-language, ...). The API client reuses them for signed fetches and
        the httpx/requests byte-download paths.

        API params: every API request the webapp's own JS issues updates the
        param-template cache for that endpoint type (e.g. 'api/post/item_list'
        vs 'api/user/detail' — each endpoint has its own param shape, and
        TikTok binds response trust to the requesting fingerprint). The cache
        is lazily filled by the scraping route and always keeps the freshest
        observation, including its msToken. The API client's own in-page
        fetches are excluded via its _inflight_fetch_urls registry (they are
        template-derived, so recycling them would compound any staleness).
        """
        if not isinstance(event, cdp.network.RequestWillBeSent):
            return
        if self._captured_request_headers is None:
            raw = event.request.headers
            self._captured_request_headers = dict(raw) if raw else {}
        url = event.request.url
        if (url.startswith('https://www.tiktok.com/api/')
                and 'device_id=' in url
                and not self.tiktok_api.is_self_issued(url)):
            params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            self.tiktok_api.cache_api_params(url, params)

    async def process_pending_responses(self, url_pattern=None):
        """Fetch bodies for ready requests and return those matching the URL pattern."""
        # Fetch bodies for all ready requests
        ready_ids = [
            rid for rid, info in self._pending_requests.items()
            if info['ready']
        ]
        for request_id in ready_ids:
            info = self._pending_requests.pop(request_id)
            try:
                result = await self._page.send(cdp.network.get_response_body(request_id))
                if isinstance(result, tuple):
                    body, base64_encoded = result[0], result[1]
                else:
                    body, base64_encoded = result.body, getattr(result, 'base64_encoded', False)
                # CDP base64-encodes binary bodies (e.g. video MP4 bytes); decode to raw bytes
                # so callers get usable data. Text bodies (JSON API responses) stay as str.
                if body and base64_encoded:
                    body = base64.b64decode(body)
                if body:
                    self._collected_responses.append({
                        'url': info['url'],
                        'body': body,
                        'response': info['response']
                    })
            except Exception:
                pass

        results = []
        remaining = []
        for resp in self._collected_responses:
            if url_pattern and url_pattern not in resp['url']:
                remaining.append(resp)
            else:
                results.append(resp)

        self._collected_responses = remaining
        return results

    async def __aenter__(self):
        # The browser-launch phase (zendriver start through session bind) is not
        # safe to run concurrently with other PyTok launches — see startup_lock.
        # Serialize it under the shared lock when one was supplied; release it
        # before account verification so a slow login/captcha doesn't block other
        # workers' startup.
        startup_lock = self._startup_lock or contextlib.nullcontext()
        async with startup_lock:
            await self._launch_browser_and_bind_session()

        # If running as a pool account, verify the profile is logged into the
        # expected identity (repairing from the cookie backup / login if needed)
        # before any scraping happens.
        if self._account is not None:
            try:
                await self._verify_account()
                # Re-derive API session tokens now that we may have injected
                # cookies or logged in.
                await self._refresh_api_tokens()
            except Exception:
                # Entry failed: __aexit__ won't run, so tear down here (which
                # also releases the account back to the pool) before re-raising.
                self._is_context_manager = True
                await self.shutdown()
                raise

        self._is_context_manager = True
        return self

    async def _launch_browser_and_bind_session(self):
        """Start the zendriver browser, load tiktok.com and bind the API session.

        This is the concurrency-sensitive part of startup (zendriver's
        free_port() is TOCTOU and the initial page-load JS context races under
        concurrent launches), so callers serialize it via the shared
        startup_lock. Kept as a discrete step so both the first build and any
        mid-run rebuild go through the same serialized path.
        """
        # Initialize zendriver state for network response tracking
        self._pending_requests = {}
        self._collected_responses = []

        # Create zendriver browser instance for PyTok's scraping
        self._zendriver_browser = await zd.start(
            headless=self._headless,
            user_data_dir=self._user_data_dir,
            browser_args=self._browser_args if self._browser_args else None,
        )

        # Get a page and set up network tracking
        self._page = await self._zendriver_browser.get('about:blank')

        # Simulate focused/active page to prevent throttling when window loses focus
        await self._page.send(cdp.emulation.set_focus_emulation_enabled(True))
        await self._page.send(cdp.page.set_web_lifecycle_state("active"))

        # TODO: test whether injecting visibility overrides into zendriver helps
        # await self._page.evaluate(self._VISIBILITY_OVERRIDE_JS)
        # await self._page.send(cdp.page.add_script_to_evaluate_on_new_document(self._VISIBILITY_OVERRIDE_JS))

        # Enable network tracking via CDP
        await self._page.send(cdp.network.enable())

        # Set up network event handlers
        self._page.add_handler(cdp.network.ResponseReceived, self._on_response)
        self._page.add_handler(cdp.network.LoadingFinished, self._on_loading_finished)

        # Capture the real request headers and per-endpoint API params off the
        # main tab's traffic (see _on_request_will_be_sent). The API client
        # reuses them for its signed fetches and the httpx/requests
        # byte-download paths. Reset on (re)launch so a rebuilt browser can't
        # serve stale templates.
        self._captured_request_headers = None
        self.tiktok_api.clear_api_param_cache()
        self._page.add_handler(cdp.network.RequestWillBeSent, self._on_request_will_be_sent)

        # Navigate to TikTok (use CDP navigate + wait_for_ready_state to avoid hanging on slow resources)
        await self._page.send(cdp.page.navigate('https://www.tiktok.com'))
        try:
            async with asyncio.timeout(self._page_load_timeout):
                await self._page.wait_for_ready_state(until='complete', timeout=self._page_load_timeout + 1)
        except (asyncio.TimeoutError, TimeoutError) as ex:
            # bare TimeoutError stringifies to '', which is useless in logs — re-raise with context
            raise TimeoutError(
                f"tiktok.com did not reach readyState 'complete' within {self._page_load_timeout}s "
                f"(pass a larger page_load_timeout if the site is just loading slowly)"
            ) from ex
        await asyncio.sleep(3)

        # The handler stays attached: headers are captured off the first
        # request, but the API param template needs a navigation that fires a
        # webapp API request, which may only happen later (account
        # verification, first profile load). Both captures are one-shot, so
        # the steady-state per-request cost is two None-checks.

        # Get user agent from zendriver page
        self._user_agent = await self._page.evaluate("navigator.userAgent")

        if self._num_sessions and self._num_sessions > 1:
            self.logger.warning(
                "num_sessions > 1 is no longer supported: the API client now shares "
                "PyTok's single main tab. Using one session."
            )

        # Bind the API client to this same main tab. Signing, fetches, network
        # capture and DOM scraping all run in this one foreground tab — no
        # background session tabs to keep alive.
        await self.tiktok_api.create_sessions(
            zendriver_browser=self._zendriver_browser,
            existing_tab=self._page,
            headers=self._captured_request_headers,
            starting_url='https://www.tiktok.com',
        )

        # TODO: test whether injecting visibility overrides into sessions helps
        # await self._inject_visibility_into_sessions()

    @classmethod
    async def from_pool(cls, accounts_pool, username: Optional[str] = None, **kwargs):
        """Acquire an account from the pool and build a PyTok bound to it.

        Marks the account in_use; it is released on shutdown / context exit.

        ```python
        pool = AccountsPool()
        async with await PyTok.from_pool(pool) as api:
            async for video in api.user(username="therock").videos():
                ...
        ```

        Args:
            accounts_pool: an accounts.AccountsPool.
            username: acquire this specific account; otherwise the least-recently
                used available one. Raises NoAccountError if none is available.
        """
        from .accounts import NoAccountError

        if username is not None:
            account = await accounts_pool.get_account(username)
        else:
            account = await accounts_pool.get_available()
        if account is None:
            raise NoAccountError(
                f"No account available"
                + (f" for username {username}" if username else "")
            )
        return cls(account=account, accounts_pool=accounts_pool, **kwargs)

    async def _inject_visibility_into_sessions(self):
        """Inject visibility API overrides into all TikTok-Api sessions.

        Uses CDP add_script_to_evaluate_on_new_document so overrides apply on
        future navigations. Does NOT call evaluate() on the current page to
        avoid disrupting already-loaded TikTok scripts like byted_acrawler.
        """
        for session in self.tiktok_api.sessions:
            try:
                await session.tab.send(
                    cdp.page.add_script_to_evaluate_on_new_document(self._VISIBILITY_OVERRIDE_JS)
                )
            except Exception as e:
                self.logger.debug(f"Failed to inject visibility overrides into session: {e}")

    async def request_delay(self):
        if self._request_delay is not None:
            await asyncio.sleep(self._request_delay)
        # Add small random jitter to look more human
        await asyncio.sleep(random.uniform(0.1, 0.5))

    async def __del__(self):
        """A basic cleanup method, called automatically from the code"""
        if not self._is_context_manager:
            self.logger.debug(
                "PyTok was shutdown improperlly. Ensure the instance is terminated with .shutdown()"
            )
            await self.shutdown()
        return

    #
    # PRIVATE METHODS
    #

    def r1(self, pattern, text):
        m = re.search(pattern, text)
        if m:
            return m.group(1)

    async def shutdown(self) -> None:
        # Persist the latest cookies and release the account back to the pool
        # before tearing down the browser (needs the live tab, so do it first).
        if getattr(self, "_account", None) is not None:
            try:
                await self._sync_cookies_to_pool()
            except Exception:
                pass
            if getattr(self, "_accounts_pool", None) is not None:
                try:
                    await self._accounts_pool.update_last_used(self._account.username)
                    if self._release_on_shutdown:
                        await self._accounts_pool.release_account(self._account.username)
                except Exception:
                    pass
        # When a persistent profile is in use, force Chrome to write cookies to
        # disk before teardown (must happen after _sync_cookies_to_pool, which
        # needs the live tab). Otherwise the profile never persists a session
        # injected/refreshed this run and re-injects from the DB backup on every
        # launch instead of coming up "from profile".
        if getattr(self, "_user_data_dir", None):
            await self._flush_cookies_to_disk()
        try:
            # Drop the API client's session reference (does not touch the main tab)
            await self.tiktok_api.close_sessions()
        except Exception:
            pass
        try:
            # Stop the zendriver browser, which owns and closes the main tab
            zendriver_browser = getattr(self, "_zendriver_browser", None)
            if zendriver_browser:
                await zendriver_browser.stop()
        except Exception:
            pass

    async def _flush_cookies_to_disk(self, grace: float = 1.5) -> None:
        """Force Chrome to persist cookies to the profile's on-disk store.

        Chrome's SQLite cookie store commits on a lazy (~30s) timer; a graceful
        Browser.close triggers an immediate flush, but the write needs a brief
        grace period to land before zendriver terminates the process (its stop()
        sends Browser.close then kills the process too fast for the flush).
        """
        browser = getattr(self, "_zendriver_browser", None)
        conn = getattr(browser, "connection", None) if browser else None
        if not conn or getattr(conn, "closed", True):
            return
        try:
            await conn.send(cdp.browser.close())
            await asyncio.sleep(grace)
        except Exception:
            pass

    async def __aexit__(self, type, value, traceback):
        await self.shutdown()

    async def refresh_sessions(self, refresh_zendriver: bool = True):
        """Refresh the API session's tokens/cookies and params in place.

        Call this when you notice API requests starting to fail consistently.
        Since the API client shares PyTok's main tab, this re-navigates the tab
        to refresh cookies and then re-derives the session's msToken and params.
        No tabs are opened or closed.

        Args:
            refresh_zendriver: If True, navigate the main page back to
                TikTok.com to refresh cookies. Defaults to True.
        """
        self.logger.info("Refreshing TikTok-Api session...")

        # Optionally refresh cookies by navigating the main page
        if refresh_zendriver:
            self.logger.debug("Refreshing cookies...")
            await self._page.send(cdp.page.navigate('https://www.tiktok.com'))
            async with asyncio.timeout(15):
                await self._page.wait_for_ready_state(until='complete', timeout=16)
            await asyncio.sleep(3)

        # Clear accumulated state
        self.request_cache = {}
        self._collected_responses = []
        self._pending_requests = {}

        # Re-derive msToken and params on the shared session (no tab churn)
        await self.tiktok_api.refresh_session_params()

        self.logger.info("Session refreshed successfully")

    async def get_ms_tokens(self, retries=3, delay=2):
        # Use CDP to get cookies from zendriver, with retry logic
        cookie_name = 'msToken'
        for attempt in range(retries):
            result = await self._page.send(cdp.network.get_cookies())
            all_cookies = result
            cookies = []
            for cookie in all_cookies:
                if cookie.name == cookie_name and cookie.secure:
                    cookies.append(cookie.value)
            if cookies:
                return cookies
            if attempt < retries - 1:
                self.logger.debug(f"msToken not found, retrying in {delay}s (attempt {attempt + 1}/{retries})")
                await asyncio.sleep(delay)
        raise Exception(f"Could not find {cookie_name} cookie after {retries} attempts")

    async def login(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 300,
        wait_for_input: bool = False
    ) -> bool:
        """Log in to TikTok with username/email and password.

        If credentials are provided, attempts automatic login. Otherwise,
        opens the login page for manual login.

        Note: TikTok often requires additional verification (email/SMS code)
        after entering credentials. When this happens, the automatic login
        will fill in the credentials and click the login button, but you'll
        need to manually complete the verification step in the browser window.
        The method will wait up to `timeout` seconds for login to complete.

        Parameters
        ----------
        username : str, optional
            TikTok username or email address
        password : str, optional
            Account password
        timeout : int, optional
            Maximum time in seconds to wait for login completion (default: 300)
        wait_for_input : bool, optional
            If True (default), waits for you to press Enter after logging in
            manually. If False, polls for login cookies until timeout.

        Returns
        -------
        bool
            True if login was successful

        Raises
        ------
        TimeoutException
            If login is not completed within the timeout period
        LoginException
            If automatic login fails (e.g., invalid credentials)
        """
        if await self._is_logged_in():
            self.logger.info("Already logged in.")
            await self._refresh_api_tokens()
            return True

        login_url = 'https://www.tiktok.com/login/phone-or-email/email'

        # Navigate to login page
        await self._page.send(cdp.page.navigate(login_url))
        async with asyncio.timeout(30):
            await self._page.wait_for_ready_state(until='complete', timeout=31)
        await asyncio.sleep(2)

        if username and password:
            return await self._automatic_login(username, password, timeout)
        else:
            return await self._manual_login(timeout, wait_for_input)

    async def _manual_login(self, timeout: int, wait_for_input: bool = False) -> bool:
        """Wait for user to complete manual login."""
        self.logger.info("Please complete the login process in the browser window...")

        if wait_for_input:
            input("Press Enter after you've logged in...")
            if not await self._is_logged_in():
                raise LoginException("Login failed - no session cookies found")
            await self._refresh_api_tokens()
            self.logger.info("Login complete.")
            return True

        start_time = time.time()
        while time.time() - start_time < timeout:
            if await self._is_logged_in():
                self.logger.info("Login successful!")
                await self._refresh_api_tokens()
                return True
            await asyncio.sleep(2)

        raise TimeoutException(f"Login not completed within {timeout} seconds")

    async def _automatic_login(self, username: str, password: str, timeout: int) -> bool:
        """Perform automatic login with credentials."""
        self.logger.info("Attempting automatic login...")

        # Find and click username field
        username_input = await self._find_login_element(
            'input[name="username"]',
            'input[placeholder*="Email" i]',
            'input[placeholder*="Username" i]',
            'input[type="text"]'
        )
        if not username_input:
            raise LoginException("Could not find username input field")

        self.logger.info("Found username field, entering username...")
        await username_input.mouse_click()
        await asyncio.sleep(0.5)

        # Use element's send_keys method
        await username_input.send_keys(username)
        await asyncio.sleep(0.5)

        # Find and click password field
        password_input = await self._find_login_element(
            'input[name="password"]',
            'input[type="password"]'
        )
        if not password_input:
            raise LoginException("Could not find password input field")

        self.logger.info("Found password field, entering password...")
        await password_input.mouse_click()
        await asyncio.sleep(0.5)

        # Use element's send_keys method
        await password_input.send_keys(password)
        await asyncio.sleep(1)

        # Find and click login button using CDP mouse events (most reliable)
        self.logger.info("Clicking login button...")
        box = await self._page.evaluate("""
            (() => {
                const btn = document.querySelector('button[data-e2e="login-button"]') ||
                           document.querySelector('button[type="submit"]');
                if (btn) {
                    const rect = btn.getBoundingClientRect();
                    return { x: rect.x + rect.width/2, y: rect.y + rect.height/2 };
                }
                return null;
            })()
        """)
        if box:
            await self._page.send(cdp.input_.dispatch_mouse_event(
                type_='mousePressed',
                x=box['x'],
                y=box['y'],
                button=cdp.input_.MouseButton.LEFT,
                click_count=1
            ))
            await self._page.send(cdp.input_.dispatch_mouse_event(
                type_='mouseReleased',
                x=box['x'],
                y=box['y'],
                button=cdp.input_.MouseButton.LEFT,
                click_count=1
            ))
        else:
            # Fallback: press Enter in password field
            self.logger.info("Login button not found, pressing Enter...")
            await password_input.send_keys('\n')

        await asyncio.sleep(3)

        # Handle captcha if it appears
        await self._handle_login_captcha()

        # Wait for login to complete
        start_time = time.time()
        check_count = 0
        while time.time() - start_time < timeout:
            check_count += 1
            # Check for login errors
            error_message = await self._check_login_error()
            if error_message:
                raise LoginException(f"Login failed: {error_message}")

            if await self._is_logged_in():
                self.logger.info("Login successful!")
                # Refresh ms_tokens after login
                await self._refresh_api_tokens()
                return True

            # Check for captcha again (may appear after initial attempt)
            await self._handle_login_captcha()

            # Log current URL periodically
            if check_count % 5 == 0:
                current_url = self._page.url
                self.logger.info(f"Waiting for login... current URL: {current_url}")

            await asyncio.sleep(2)

        raise TimeoutException(f"Login not completed within {timeout} seconds")

    async def _find_login_element(self, *selectors):
        """Try multiple selectors to find a login form element."""
        for selector in selectors:
            try:
                element = await self._page.select(selector, timeout=2)
                if element:
                    return element
            except Exception:
                continue
        return None

    async def _handle_login_captcha(self):
        """Check for and solve captcha during login."""
        from .api.base import CAPTCHA_TEXTS

        for text in CAPTCHA_TEXTS:
            try:
                element = await self._page.find(text, timeout=1)
                if element:
                    self.logger.info(f"Captcha detected during login: '{text}'")
                    if self._manual_captcha_solves:
                        input("Press Enter after solving the captcha manually...")
                        await asyncio.sleep(1)
                        return
                    # Use the Base class captcha solver
                    from .api.base import Base
                    base = Base()
                    base.parent = self
                    try:
                        await base.solve_captcha()
                        self.logger.info("Captcha solve attempt completed")
                    except Exception as e:
                        self.logger.warning(f"Captcha solve failed: {e}")
                    await asyncio.sleep(2)
                    return
            except Exception as e:
                self.logger.debug(f"Error checking for captcha text '{text}': {e}")
                continue

    async def _check_login_error(self) -> Optional[str]:
        """Check for login error messages on the page."""
        # Look for error messages in specific error containers
        error_selectors = [
            '[class*="error" i]',
            '[class*="alert" i]',
            '[data-e2e*="error" i]',
        ]
        error_texts = [
            "incorrect password",
            "invalid username",
            "account doesn't exist",
            "too many attempts",
            "something went wrong",
            "please check your password",
        ]

        for selector in error_selectors:
            try:
                elements = await self._page.select_all(selector, timeout=0.5)
                for element in elements:
                    if hasattr(element, 'text') and element.text:
                        text_lower = element.text.lower()
                        for error_text in error_texts:
                            if error_text in text_lower:
                                return element.text
            except Exception:
                continue
        return None

    async def _refresh_api_tokens(self):
        """Refresh msToken on TikTok-Api sessions after login.

        Since the browser is shared, cookies are already shared across all tabs.
        We just need to update each session's ms_token field.
        """
        try:
            for session in self.tiktok_api.sessions:
                cookies = await self.tiktok_api.get_session_cookies(session)
                ms_token = cookies.get("msToken")
                if ms_token:
                    session.ms_token = ms_token
            self.logger.debug("Refreshed msToken on API sessions")
        except Exception as e:
            self.logger.warning(f"Failed to refresh API tokens: {e}")

    async def _is_logged_in(self) -> bool:
        """Check if user is logged in by looking for session cookies."""
        result = await self._page.send(cdp.network.get_cookies())
        cookie_names = {cookie.name for cookie in result}
        # TikTok sets these cookies when logged in
        login_cookies = {'sessionid', 'sid_tt', 'sessionid_ss'}
        return bool(cookie_names & login_cookies)

    #
    # ACCOUNT IDENTITY & VERIFICATION
    #

    @staticmethod
    def _extract_identity_fields(user: dict) -> Optional[dict]:
        """Pull uid/secUid/uniqueId out of an app-context user object, tolerating
        key-name variation across TikTok webapp versions."""
        if not isinstance(user, dict):
            return None
        uid = user.get('uid') or user.get('userId') or user.get('id')
        sec_uid = user.get('secUid') or user.get('sec_uid')
        unique_id = user.get('uniqueId') or user.get('unique_id')
        nickname = user.get('nickName') or user.get('nickname')
        if uid or sec_uid or unique_id:
            return {
                'user_id': str(uid) if uid else None,
                'sec_uid': sec_uid,
                'unique_id': unique_id,
                'nickname': nickname,
            }
        return None

    async def _get_logged_in_identity(self, navigate: bool = False) -> Optional[dict]:
        """Read the currently logged-in account's on-platform identity from the
        page's app-context rehydration JSON.

        Returns {'user_id', 'sec_uid', 'unique_id', 'nickname'} for the logged-in
        account, or None if the page shows no logged-in user. This is the
        ground-truth identity check — cookies / a profile dir only prove that
        *someone* is logged in, not *who*.
        """
        from .helpers import extract_tag_contents

        if navigate:
            await self._page.send(cdp.page.navigate('https://www.tiktok.com'))
            async with asyncio.timeout(self._page_load_timeout):
                await self._page.wait_for_ready_state(
                    until='complete', timeout=self._page_load_timeout + 1
                )
            await asyncio.sleep(2)

        try:
            html = await self._page.get_content()
            data = json.loads(extract_tag_contents(html))
        except Exception as e:
            self.logger.debug(f"Identity check: could not parse rehydration JSON: {e}")
            return None

        scope = data.get('__DEFAULT_SCOPE__', {}) if isinstance(data, dict) else {}
        app_context = scope.get('webapp.app-context', {}) or {}
        identity = self._extract_identity_fields(app_context.get('user') or {})
        if identity:
            return identity

        # No logged-in user found where we expect it. Log the app-context keys so
        # we can tighten this against a real logged-in session if the shape moved.
        self.logger.debug(
            f"Identity check: no user in app-context (keys: {list(app_context.keys())})"
        )
        return None

    async def _snapshot_cookies(self) -> list:
        """Read all cookies from the browser as plain dicts for DB backup."""
        result = await self._page.send(cdp.network.get_cookies())
        cookies = []
        for c in result:
            cookies.append({
                'name': c.name,
                'value': c.value,
                'domain': c.domain,
                'path': c.path,
                'secure': bool(c.secure),
                'httpOnly': bool(c.http_only),
                'sameSite': c.same_site.value if getattr(c, 'same_site', None) else None,
                'expires': c.expires if getattr(c, 'expires', None) else None,
            })
        return cookies

    async def _inject_cookies(self, cookies: list) -> None:
        """Inject stored cookie dicts into the browser via CDP."""
        if not cookies:
            return
        same_site_map = {
            'strict': cdp.network.CookieSameSite.STRICT,
            'lax': cdp.network.CookieSameSite.LAX,
            'none': cdp.network.CookieSameSite.NONE,
            'no_restriction': cdp.network.CookieSameSite.NONE,
        }
        params = []
        for c in cookies:
            name, value = c.get('name'), c.get('value')
            if name is None or value is None:
                continue
            # A stored `None`/'' sameSite means the cookie had NO SameSite
            # attribute — that must be re-injected as *unset*, not as an explicit
            # SameSite=None. Forcing SameSite=None breaks TikTok's session cookies:
            # the injected sessionid then isn't honoured and app-context stays
            # anonymous (verified against gmail's own known-good cookies).
            raw_same_site = c.get('sameSite')
            same_site = same_site_map.get(str(raw_same_site).lower()) if raw_same_site else None
            domain = c.get('domain')
            # CDP's expires is a TimeSinceEpoch (a float subclass with .to_json());
            # a raw float would blow up serialization ('float' has no to_json).
            raw_expires = c.get('expires')
            expires = None
            if isinstance(raw_expires, (int, float)) and raw_expires > 0:
                expires = cdp.network.TimeSinceEpoch(raw_expires)
            params.append(cdp.network.CookieParam(
                name=name,
                value=value,
                # A domainless cookie needs a url to anchor to; supply one.
                url=None if domain else 'https://www.tiktok.com',
                domain=domain,
                path=c.get('path', '/'),
                secure=c.get('secure'),
                http_only=c.get('httpOnly'),
                same_site=same_site,
                expires=expires,
            ))
        if params:
            await self._page.send(cdp.network.set_cookies(params))

    async def _verify_account(self) -> None:
        """Verify the attached profile is logged into the expected account, and
        repair from the cookie backup / login flow if not.

        Runs during __aenter__ when an account is attached. The account's
        `user_id` is the referee: we never scrape under the wrong identity.
        """
        account, pool = self._account, self._accounts_pool

        async def _identity_matches() -> Optional[dict]:
            ident = await self._get_logged_in_identity()
            if not ident:
                return None
            if account.user_id and ident.get('user_id') and ident['user_id'] != account.user_id:
                self.logger.warning(
                    f"Profile for {account.username} is logged into a DIFFERENT account "
                    f"(expected uid {account.user_id}, got {ident['user_id']}/{ident.get('unique_id')})"
                )
                return None
            return ident

        async def _capture_identity(ident: dict) -> None:
            # First login for this account, or a refresh of a partial record.
            if pool and (not account.user_id or not account.unique_id):
                account.user_id = ident.get('user_id') or account.user_id
                account.sec_uid = ident.get('sec_uid') or account.sec_uid
                account.unique_id = ident.get('unique_id') or account.unique_id
                await pool.set_identity(
                    account.username, account.user_id, account.sec_uid, account.unique_id
                )

        async def _confirm(ident: dict) -> None:
            await _capture_identity(ident)
            self._identity_confirmed = True
            await self._sync_cookies_to_pool()

        if self._force_relogin:
            # Recovery path: drop the (stale) session so login() can't short-circuit
            # on invalid cookies, then go straight to a fresh credentialed login.
            self.logger.info(f"force_relogin: clearing session for {account.username}")
            try:
                await self._page.send(cdp.network.clear_browser_cookies())
            except Exception as e:
                self.logger.debug(f"clear_browser_cookies failed: {e}")
        else:
            # 1) Profile as-is — is the right account already logged in?
            if await self._is_logged_in():
                ident = await _identity_matches()
                if ident:
                    self.logger.info(
                        f"Verified account {account.display_name} "
                        f"(uid={ident.get('user_id')}) from profile"
                    )
                    await _confirm(ident)
                    return

            # 2) Repair from the DB cookie backup, if we have one.
            if account.cookies:
                self.logger.info(f"Injecting cookie backup for {account.username}")
                await self._inject_cookies(account.cookies)
                ident = await self._identity_after_reload(_identity_matches)
                if ident:
                    self.logger.info(f"Repaired session for {account.display_name} from cookie backup")
                    await _confirm(ident)
                    return

        # 3) Fall back to an interactive/credentialed login.
        self.logger.info(f"No valid session for {account.username}; running login flow")
        ok = await self.login(username=account.username, password=account.password)
        if not ok:
            if pool:
                await pool.set_active(account.username, False, "Login failed during verification")
            raise LoginException(f"Could not log in account {account.username}")

        # login() short-circuits on the presence of session cookies, which can be
        # stale server-side (logged in per cookies, but app-context has no user).
        # Require a real, matching identity read before trusting the session.
        ident = await self._get_logged_in_identity(navigate=True)
        if not ident:
            if pool:
                await pool.set_active(
                    account.username, False,
                    "Session has cookies but no logged-in user (expired/invalid) — needs re-login",
                )
            raise LoginException(
                f"Account {account.username} appears logged in by cookies but TikTok "
                f"shows no user (session expired/invalid); clear its profile and re-login"
            )
        if account.user_id and ident.get('user_id') and ident['user_id'] != account.user_id:
            raise LoginException(
                f"Logged in as {ident.get('unique_id')} (uid {ident['user_id']}) "
                f"but account {account.username} expects uid {account.user_id}"
            )
        await _confirm(ident)

    async def _identity_after_reload(self, matcher) -> Optional[dict]:
        """Reload tiktok.com (so injected cookies take effect) then run matcher."""
        await self._page.send(cdp.page.navigate('https://www.tiktok.com'))
        try:
            async with asyncio.timeout(self._page_load_timeout):
                await self._page.wait_for_ready_state(
                    until='complete', timeout=self._page_load_timeout + 1
                )
        except (asyncio.TimeoutError, TimeoutError):
            pass
        await asyncio.sleep(2)
        return await matcher()

    async def _sync_cookies_to_pool(self) -> None:
        """Snapshot the live browser cookies back to the account's DB backup.

        Only runs once the session's identity has been confirmed — otherwise a
        stale/unverified session (valid-looking cookies, no real login) could
        overwrite and degrade a good cookie backup.
        """
        if not (self._account and self._accounts_pool and self._identity_confirmed):
            return
        try:
            cookies = await self._snapshot_cookies()
            login_cookies = {'sessionid', 'sid_tt', 'sessionid_ss'}
            if any(c['name'] in login_cookies for c in cookies):
                self._account.cookies = cookies
                await self._accounts_pool.update_cookies(self._account.username, cookies)
                await self._accounts_pool.set_active(self._account.username, True, None)
        except Exception as e:
            self.logger.debug(f"Failed to sync cookies to pool: {e}")
