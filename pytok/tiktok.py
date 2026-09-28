import asyncio
import contextlib
import json
import logging
import os
import random
import re
import time
from typing import Optional, Union
from urllib.parse import parse_qs, urlparse

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .api.hashtag import Hashtag
from .api.search import Search
from .api.sound import Sound
from .api.trending import Trending
from .api.user import User
from .api.video import Video
from .exceptions import *
from .tiktok_api import TikTokApiClient
from .utils import LOGGER_NAME

os.environ["no_proxy"] = "127.0.0.1,localhost"

BASE_URL = "https://m.tiktok.com/"
DESKTOP_BASE_URL = "https://www.tiktok.com/"


class PyTok:
    _is_context_manager = False
    # Numbers the loggers of sessions that have no account to name them by.
    _anonymous_sessions = 0

    # Firefox throttles timers in background and occluded windows, which stalls a feed
    # being scrolled in a window that isn't in front (every worker but one, in a pool).
    _FIREFOX_PREFS = {
        "dom.timeout.enable_budget_timer_throttling": False,
        "dom.min_background_timeout_value": 4,
        "widget.windows.window_occlusion_tracking.enabled": False,
        "browser.sessionstore.resume_from_crash": False,
    }

    # Headers of the captured request that belong to it alone. Replayed on a download
    # from the video CDN, the tiktok.com host header gets it a 403.
    _UNREPLAYABLE_HEADERS = frozenset({'host', 'cookie', 'connection', 'content-length'})

    # Firefox's error when TikTok closes the connection without a response, which it does
    # to every page for a session it is throttling.
    _DROPPED_CONNECTION_ERROR = "NS_ERROR_NET_EMPTY_RESPONSE"

    # Where a persistent profile keeps the fingerprint it was first given.
    _FINGERPRINT_FILE = "pytok-fingerprint.json"

    def __init__(
            self,
            logging_level: Optional[int] = None,
            request_delay: Optional[int] = 0,
            headless: Union[bool, str] = False,
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
            manual_login: bool = False,
            login_timeout: int = 300,
            startup_lock: Optional[asyncio.Lock] = None,
    ):
        """The PyTok class. Used to interact with TikTok.

        ##### Parameters
        * logging_level: The logging level for this session's logger, optional
            These are the standard python logging module's levels. Each session logs
            to its own child of the "PyTok" logger, named after its account, so this
            never changes the level of other sessions or of "PyTok" itself. None
            (default) inherits the level from "PyTok".

        * request_delay: The amount of time in seconds to wait before making a request, optional
            This is used to throttle your own requests as you may end up making too
            many requests to TikTok for your IP.

        * num_sessions: Number of browser sessions to create (used by the API client), optional

        * headless: Run without a visible window, optional. On Linux pass "virtual" to
            run headful inside Xvfb instead, which TikTok treats better than true headless.

        * user_data_dir: Path to a persistent Firefox profile directory, optional
            If not provided, uses a fresh profile each session. A persistent profile also
            keeps the browser fingerprint it was first given, so the account keeps
            appearing from the same device.
            Note: Don't use a profile that's open in another browser.

        * browser_args: Additional Firefox command-line arguments, optional

        * page_load_timeout: Seconds to wait for a navigation to finish loading, optional.
            A cold start against a large persistent profile plus a slow TikTok homepage
            can take a while; raise this if setup keeps timing out.

        * account: An accounts.Account to run this session as, optional. When set,
            its persistent browser profile dir is used (unless user_data_dir is given
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
            the browser-launch phase (browser start + first TikTok page load), so
            N browsers don't all cold-start against TikTok at the same instant. It
            is released before account verification so a slow login/captcha on one
            worker doesn't block the others' startup. None (default) = no
            serialization (standalone use).
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
        # When True, verification hands the login to whoever is at the browser instead of
        # driving it from the stored credentials. The automatic flow cannot get past a
        # captcha the solver fails to read, which is a dead end for an account that needs
        # one; a person can type the credentials and solve it.
        # not _manual_login: that is the method this flag ends up calling
        self._use_manual_login = manual_login
        self._login_timeout = login_timeout
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
        self._browser_args = list(browser_args or [])

        self.logger = self._session_logger(account)
        if logging_level is not None:
            self.logger.setLevel(logging_level)

        self.request_cache = {}

        self.tiktok_api = TikTokApiClient(
            logging_level=logging_level
        )

    @classmethod
    def _session_logger(cls, account) -> logging.Logger:
        """A child of the "PyTok" logger named after this session's account, so
        concurrent sessions' lines can be told apart."""
        if account is not None:
            # Dots would split an email username into further logger levels.
            name = account.display_name.replace(".", "_")
        else:
            cls._anonymous_sessions += 1
            name = f"session{cls._anonymous_sessions}"
        return logging.getLogger(LOGGER_NAME).getChild(name)

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

    def _on_request(self, request):
        """Record tracked requests, and capture headers and per-endpoint API params.

        Headers: taken from the first outgoing request (user-agent, accept-language,
        ...). The httpx/requests byte-download paths reuse them.

        API params: every API request the webapp's own JS issues updates the
        param-template cache for that endpoint type (e.g. 'api/post/item_list'
        vs 'api/user/detail' — each endpoint has its own param shape, and
        TikTok binds response trust to the requesting fingerprint). The cache
        is lazily filled by the scraping route and always keeps the freshest
        observation. The API client's own in-page fetches are excluded via its
        _inflight_fetch_urls registry (they are template-derived, so recycling
        them would compound any staleness).
        """
        if self._captured_request_headers is None:
            self._captured_request_headers = {
                k: v for k, v in request.headers.items()
                if k.lower() not in self._UNREPLAYABLE_HEADERS
            }
        url = request.url
        if self._should_track_url(url):
            self._pending_requests[request] = {'url': url, 'ready': False}
        if (url.startswith('https://www.tiktok.com/api/')
                and 'device_id=' in url
                and not self.tiktok_api.is_self_issued(url)):
            params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            self.tiktok_api.cache_api_params(url, params)

    def _on_request_finished(self, request):
        info = self._pending_requests.get(request)
        if info is None:
            return
        info['ready'] = True
        task = asyncio.ensure_future(self._bank_response(request, info))
        self._body_reads.add(task)
        task.add_done_callback(self._body_reads.discard)

    def _on_request_failed(self, request):
        self._pending_requests.pop(request, None)

    async def _bank_response(self, request, info):
        """Read a finished tracked response's body into _collected_responses."""
        try:
            response = await request.response()
            if response is None:
                return
            body = await response.body()
            server_addr = await response.server_addr()
        except Exception:
            return
        finally:
            self._pending_requests.pop(request, None)
        if not body:
            return
        headers = response.headers
        content_type = headers.get('content-type', '')
        # Callers parse text bodies (JSON API responses) as str and treat media as bytes.
        if any(t in content_type for t in ('json', 'text', 'javascript')):
            body = body.decode('utf-8', errors='replace')
        self._collected_responses.append({
            'url': info['url'],
            'body': body,
            'status': response.status,
            'headers': headers,
            'server_addr': server_addr.get('ipAddress') if server_addr else None,
        })

    def seen_request_urls(self, url_pattern):
        """URLs of tracked requests seen since the last clear, matching url_pattern.

        Covers requests whose bodies are still pending as well as those already
        collected, so callers can read what the page asked for even when the
        response body itself is gone or unusable.
        """
        urls = [info['url'] for info in self._pending_requests.values()]
        urls += [resp['url'] for resp in self._collected_responses]
        return [url for url in urls if url_pattern in url]

    async def collect_pending_response_bodies(self, timeout: float = 10):
        """Wait for the body reads of every request that has already finished.

        Bodies are read as each request finishes, so this only waits for reads
        still in flight. Call it before reading _collected_responses when the
        responses a page just fetched matter. Unlike process_pending_responses
        this consumes nothing, so later filtered reads still find everything.
        """
        if not self._body_reads:
            return
        await asyncio.wait(list(self._body_reads), timeout=timeout)

    async def process_pending_responses(self, url_pattern=None):
        """Return (and consume) the collected responses matching the URL pattern."""
        await self.collect_pending_response_bodies()

        results = []
        remaining = []
        for resp in self._collected_responses:
            if url_pattern and url_pattern not in resp['url']:
                remaining.append(resp)
            else:
                results.append(resp)

        self._collected_responses = remaining
        return results

    async def navigate(self, url: str, wait_until: str = "commit"):
        """Point the page at url, waiting only as far as wait_until.

        A navigation cut short by another one (TikTok's own script redirecting, say)
        is not an error here: the page is wherever the site sent it.
        """
        try:
            await self._page.goto(url, wait_until=wait_until,
                                  timeout=self._page_load_timeout * 1000)
        except PlaywrightTimeoutError as ex:
            raise TimeoutError(
                f"{url} did not reach '{wait_until}' within {self._page_load_timeout}s"
            ) from ex
        except PlaywrightError as ex:
            if "NS_BINDING_ABORTED" in str(ex) or "interrupted by another navigation" in str(ex):
                self.logger.debug(f"Navigation to {url} was superseded: {ex}")
            elif self._DROPPED_CONNECTION_ERROR in str(ex):
                raise ConnectionDroppedException(
                    f"TikTok dropped the connection loading {url}"
                ) from ex
            else:
                raise

    async def wait_for_load(self, timeout: Optional[float] = None):
        """Wait for the current page's load event, raising TimeoutError past timeout."""
        timeout = self._page_load_timeout if timeout is None else timeout
        try:
            await self._page.wait_for_load_state("load", timeout=timeout * 1000)
        except PlaywrightTimeoutError as ex:
            raise TimeoutError(f"{self._page.url} did not finish loading within {timeout}s") from ex

    async def __aenter__(self):
        # The browser-launch phase (browser start through session bind) is
        # serialized under the shared lock when one was supplied; release it
        # before account verification so a slow login/captcha doesn't block other
        # workers' startup.
        startup_lock = self._startup_lock or contextlib.nullcontext()
        try:
            async with startup_lock:
                await self._launch_browser_and_bind_session()
        except Exception:
            # Tear down the half-started browser and release the account.
            self._is_context_manager = True
            await self.shutdown()
            raise

        # If running as a pool account, verify the profile is logged into the
        # expected identity (repairing from the cookie backup / login if needed)
        # before any scraping happens.
        if self._account is not None:
            try:
                await self._verify_account()
            except Exception:
                # Entry failed: __aexit__ won't run, so tear down here (which
                # also releases the account back to the pool) before re-raising.
                self._is_context_manager = True
                await self.shutdown()
                raise

        self._is_context_manager = True
        return self

    def _fingerprint_preset(self):
        """The fingerprint preset for this profile, pinned on first use.

        A logged-in account that turns up on a new device every session is a bot
        signal, so a persistent profile keeps the preset it was first given.
        Without a profile there is nothing to keep, so each session draws a new one.
        """
        from camoufox.fingerprints import get_random_preset

        if not self._user_data_dir:
            return get_random_preset()
        path = os.path.join(self._user_data_dir, self._FINGERPRINT_FILE)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        preset = get_random_preset()
        os.makedirs(self._user_data_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump(preset, f)
        return preset

    async def _launch_browser_and_bind_session(self):
        """Start camoufox, load tiktok.com and bind the API session.

        Kept as a discrete step so both the first build and any mid-run rebuild
        go through the same (optionally serialized) path.
        """
        self._pending_requests = {}
        self._collected_responses = []
        self._body_reads = set()

        # main_world_eval lets the API client reach the webapp's signer; everything
        # else evaluates in Playwright's isolated world, which the page cannot see.
        profile = (
            {'persistent_context': True, 'user_data_dir': self._user_data_dir}
            if self._user_data_dir else {}
        )
        self._camoufox = AsyncCamoufox(
            headless=self._headless,
            main_world_eval=True,
            fingerprint_preset=self._fingerprint_preset(),
            firefox_user_prefs=self._FIREFOX_PREFS,
            args=self._browser_args or None,
            **profile,
        )
        browser_or_context = await self._camoufox.__aenter__()
        if self._user_data_dir:
            self._context = browser_or_context
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        else:
            self._context = await browser_or_context.new_context()
            self._page = await self._context.new_page()

        # Capture the real request headers and per-endpoint API params off the
        # page's traffic (see _on_request). Reset on (re)launch so a rebuilt
        # browser can't serve stale templates.
        self._captured_request_headers = None
        self.tiktok_api.clear_api_param_cache()
        self._page.on("request", self._on_request)
        self._page.on("requestfinished", self._on_request_finished)
        self._page.on("requestfailed", self._on_request_failed)

        try:
            await self.navigate('https://www.tiktok.com', wait_until='load')
        except TimeoutError as ex:
            raise TimeoutError(
                f"tiktok.com did not finish loading within {self._page_load_timeout}s "
                f"(pass a larger page_load_timeout if the site is just loading slowly)"
            ) from ex
        await asyncio.sleep(3)

        self._user_agent = await self._page.evaluate("navigator.userAgent")

        if self._num_sessions and self._num_sessions > 1:
            self.logger.warning(
                "num_sessions > 1 is no longer supported: the API client now shares "
                "PyTok's single page. Using one session."
            )

        # Signing, fetches, network capture and DOM scraping all run in this one page.
        await self.tiktok_api.create_sessions(
            context=self._context,
            existing_page=self._page,
            headers=self._captured_request_headers,
            starting_url='https://www.tiktok.com',
        )

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
                "No account available"
                + (f" for username {username}" if username else "")
            )
        return cls(account=account, accounts_pool=accounts_pool, **kwargs)

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
        try:
            # Drop the API client's session reference (does not touch the page)
            await self.tiktok_api.close_sessions()
        except Exception:
            pass
        try:
            # Closing a persistent context is what writes its cookies to the profile.
            camoufox = getattr(self, "_camoufox", None)
            if camoufox is not None:
                await camoufox.__aexit__(None, None, None)
        except Exception:
            pass

    async def __aexit__(self, type, value, traceback):
        await self.shutdown()

    async def refresh_sessions(self, navigate: bool = True):
        """Refresh the API session's cookies and state in place.

        Call this when you notice API requests starting to fail consistently.
        Since the API client shares PyTok's page, this re-navigates the page to
        refresh cookies and the signer. No pages are opened or closed.

        Args:
            navigate: If True, navigate the page back to TikTok.com to refresh
                cookies. Defaults to True.
        """
        self.logger.info("Refreshing API session...")

        if navigate:
            self.logger.debug("Refreshing cookies...")
            await self.navigate('https://www.tiktok.com')
            await self.wait_for_load(15)
            await asyncio.sleep(3)

        # Clear accumulated state
        self.request_cache = {}
        self._collected_responses = []
        self._pending_requests = {}

        await self.tiktok_api.refresh_session_params()

        self.logger.info("Session refreshed successfully")

    async def get_ms_tokens(self, retries=3, delay=2):
        cookie_name = 'msToken'
        for attempt in range(retries):
            cookies = [
                c['value'] for c in await self._context.cookies()
                if c['name'] == cookie_name and c['secure']
            ]
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
            If True, waits for you to press Enter after logging in manually.
            If False (default), polls for login cookies until timeout — which is
            what a caller with no console attached needs.

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
            return True

        login_url = 'https://www.tiktok.com/login/phone-or-email/email'

        await self.navigate(login_url)
        await self.wait_for_load(30)
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
            self.logger.info("Login complete.")
            return True

        start_time = time.time()
        while time.time() - start_time < timeout:
            if await self._is_logged_in():
                self.logger.info("Login successful!")
                return True
            await asyncio.sleep(2)

        raise TimeoutException(f"Login not completed within {timeout} seconds")

    async def _automatic_login(self, username: str, password: str, timeout: int) -> bool:
        """Perform automatic login with credentials."""
        self.logger.info("Attempting automatic login...")

        username_input = await self._find_login_element(
            'input[name="username"]',
            'input[placeholder*="Email" i]',
            'input[placeholder*="Username" i]',
            'input[type="text"]'
        )
        if not username_input:
            raise LoginException("Could not find username input field")

        self.logger.info("Found username field, entering username...")
        await username_input.click()
        await asyncio.sleep(0.5)
        await self._page.keyboard.type(username, delay=random.uniform(60, 140))
        await asyncio.sleep(0.5)

        password_input = await self._find_login_element(
            'input[name="password"]',
            'input[type="password"]'
        )
        if not password_input:
            raise LoginException("Could not find password input field")

        self.logger.info("Found password field, entering password...")
        await password_input.click()
        await asyncio.sleep(0.5)
        await self._page.keyboard.type(password, delay=random.uniform(60, 140))
        await asyncio.sleep(1)

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
            await self._page.mouse.click(box['x'], box['y'])
        else:
            self.logger.info("Login button not found, pressing Enter...")
            await self._page.keyboard.press("Enter")

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
            element = self._page.locator(selector).first
            try:
                await element.wait_for(state="visible", timeout=2000)
                return element
            except Exception:
                continue
        return None

    async def _handle_login_captcha(self):
        """Check for and solve captcha during login."""
        from .api.base import Base

        base = Base()
        base.parent = self
        if not await base._is_captcha_visible():
            return
        self.logger.info("Captcha detected during login")
        if self._manual_captcha_solves:
            input("Press Enter after solving the captcha manually...")
            await asyncio.sleep(1)
            return
        try:
            await base.solve_captcha()
            self.logger.info("Captcha solve attempt completed")
        except Exception as e:
            self.logger.warning(f"Captcha solve failed: {e}")
        await asyncio.sleep(2)

    _LOGIN_ERROR_JS = """
    (() => {
      const errorTexts = %s;
      const sel = '[class*="error" i], [class*="alert" i], [data-e2e*="error" i]';
      for (const el of document.querySelectorAll(sel)) {
        const text = (el.innerText || '').trim();
        if (text && errorTexts.some(e => text.toLowerCase().includes(e))) return text;
      }
      return null;
    })()
    """ % json.dumps([
        "incorrect password",
        "invalid username",
        "account doesn't exist",
        "too many attempts",
        "something went wrong",
        "please check your password",
    ])

    async def _check_login_error(self) -> Optional[str]:
        """Check for login error messages on the page."""
        try:
            return await self._page.evaluate(self._LOGIN_ERROR_JS)
        except Exception:
            return None

    async def _is_logged_in(self) -> bool:
        """Check if user is logged in by looking for session cookies."""
        cookie_names = {c['name'] for c in await self._context.cookies()}
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
            await self.navigate('https://www.tiktok.com')
            await self.wait_for_load()
            await asyncio.sleep(2)

        try:
            html = await self._page.content()
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
        return [
            {
                'name': c['name'],
                'value': c['value'],
                'domain': c['domain'],
                'path': c['path'],
                'secure': bool(c['secure']),
                'httpOnly': bool(c['httpOnly']),
                'sameSite': c.get('sameSite'),
                # Playwright reports a session cookie as expiring at -1.
                'expires': c['expires'] if c.get('expires', -1) > 0 else None,
            }
            for c in await self._context.cookies()
        ]

    async def _inject_cookies(self, cookies: list) -> None:
        """Inject stored cookie dicts into the browser."""
        same_site_map = {'strict': 'Strict', 'lax': 'Lax', 'none': 'None', 'no_restriction': 'None'}
        params = []
        for c in cookies or []:
            name, value = c.get('name'), c.get('value')
            if name is None or value is None:
                continue
            param = {'name': name, 'value': value}
            domain = c.get('domain')
            if domain:
                param['domain'] = domain
                param['path'] = c.get('path') or '/'
            else:
                # A domainless cookie needs a url to anchor to.
                param['url'] = 'https://www.tiktok.com'
            if c.get('secure') is not None:
                param['secure'] = bool(c['secure'])
            if c.get('httpOnly') is not None:
                param['httpOnly'] = bool(c['httpOnly'])
            # A stored `None`/'' sameSite means the cookie had NO SameSite
            # attribute, and it must be re-injected as unset rather than as an
            # explicit SameSite=None: TikTok doesn't honour a session cookie
            # whose SameSite was forced.
            same_site = same_site_map.get(str(c.get('sameSite')).lower()) if c.get('sameSite') else None
            if same_site:
                param['sameSite'] = same_site
            expires = c.get('expires')
            if isinstance(expires, (int, float)) and expires > 0:
                param['expires'] = float(expires)
            params.append(param)
        if params:
            await self._context.add_cookies(params)

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
                await self._context.clear_cookies()
            except Exception as e:
                self.logger.debug(f"clear_cookies failed: {e}")
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

        # 3) Fall back to an interactive/credentialed login. Withholding the credentials is
        # what selects login()'s manual path, so a person drives the whole flow.
        self.logger.info(f"No valid session for {account.username}; running login flow")
        if self._use_manual_login:
            self.logger.info(
                f"Manual login: complete the sign-in for {account.username} in the browser "
                f"window (waiting up to {self._login_timeout}s)"
            )
            ok = await self.login(timeout=self._login_timeout)
        else:
            ok = await self.login(username=account.username, password=account.password,
                                  timeout=self._login_timeout)
        if not ok:
            if pool:
                await pool.set_active(account.username, False, "Login failed during verification")
            raise LoginException(f"Could not log in account {account.username}")

        # login() short-circuits on the presence of session cookies, which can be
        # stale server-side (logged in per cookies, but app-context has no user).
        # Require a real, matching identity read before trusting the session.
        #
        # Retried, because a just-completed login is still settling: TikTok redirects to the
        # home page after sign-in and the app context carries no user until that lands. A
        # single read here fails on a login that in fact succeeded, and the account is then
        # marked inactive with a session that works -- which no later run can repair, since
        # the cookies it left behind make login() short-circuit on every retry.
        ident = await self._identity_with_retries(
            lambda: self._get_logged_in_identity(navigate=True)
        )
        if not ident:
            # Drop the cookies we just judged unusable. Leaving them made the account
            # unrecoverable: they are enough for login() to short-circuit on the next run, so
            # every later attempt reported success without ever showing a login form, failed
            # this same check, and disabled the account again.
            try:
                await self._context.clear_cookies()
                self.logger.info(
                    f"Cleared the unusable session for {account.username} so the next login "
                    f"starts from a real sign-in rather than short-circuiting on these cookies"
                )
            except Exception as e:
                self.logger.debug(f"clear_cookies failed: {e}")
            if pool:
                await pool.set_active(
                    account.username, False,
                    "Session has cookies but no logged-in user (expired/invalid) — needs re-login",
                )
            raise LoginException(
                f"Account {account.username} appears logged in by cookies but TikTok "
                f"shows no user (session expired/invalid); the stale cookies have been "
                f"cleared, so re-running the login will present a sign-in form"
            )
        if account.user_id and ident.get('user_id') and ident['user_id'] != account.user_id:
            raise LoginException(
                f"Logged in as {ident.get('unique_id')} (uid {ident['user_id']}) "
                f"but account {account.username} expects uid {account.user_id}"
            )
        await _confirm(ident)

    async def _identity_with_retries(self, read, attempts: int = 5, delay: int = 3) -> Optional[dict]:
        """Read the logged-in identity, retrying while a fresh session settles.

        Returns the first non-empty read, or None once the attempts run out. The wait is
        short and only paid on the failing path, where the alternative is disabling an
        account whose session is fine.
        """
        for attempt in range(1, attempts + 1):
            ident = await read()
            if ident:
                if attempt > 1:
                    self.logger.info(f"Identity readable on attempt {attempt}")
                return ident
            if attempt < attempts:
                self.logger.info(
                    f"No identity yet (attempt {attempt}/{attempts}); the session may still "
                    f"be settling, retrying in {delay}s"
                )
                await asyncio.sleep(delay)
        return None

    async def _identity_after_reload(self, matcher) -> Optional[dict]:
        """Reload tiktok.com (so injected cookies take effect) then run matcher."""
        try:
            await self.navigate('https://www.tiktok.com')
            await self.wait_for_load()
        except TimeoutError:
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
