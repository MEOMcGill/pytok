import asyncio
import base64
import json
import logging
import os
import re
import time
from typing import Optional

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
    user = User
    search = Search
    sound = Sound
    hashtag = Hashtag
    video = Video
    trending = Trending
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
        """
        # assert headless is False, "Running in headless currently does not work reliably."

        self._headless = headless
        self._request_delay = request_delay
        self._manual_captcha_solves = manual_captcha_solves
        self._log_captcha_solves = log_captcha_solves
        self._num_sessions = num_sessions
        self._user_data_dir = user_data_dir
        # Merge browser args: use defaults unless explicitly disabled with empty list
        if browser_args is None:
            self._browser_args = self._DEFAULT_BROWSER_ARGS.copy()
        elif browser_args == []:
            self._browser_args = []
        else:
            self._browser_args = self._DEFAULT_BROWSER_ARGS + browser_args

        self.logger.setLevel(logging_level)

        # Add classes from the api folder
        User.parent = self
        Search.parent = self
        Sound.parent = self
        Hashtag.parent = self
        Video.parent = self
        Trending.parent = self

        self.request_cache = {}

        # Create zendriver-based TikTokApi instance for API requests
        self.tiktok_api = ZendriverTikTokApi(
            logging_level=logging_level
        )


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

        # Navigate to TikTok (use CDP navigate + wait_for_ready_state to avoid hanging on slow resources)
        await self._page.send(cdp.page.navigate('https://www.tiktok.com'))
        async with asyncio.timeout(10):
            await self._page.wait_for_ready_state(until='complete', timeout=11)
        await asyncio.sleep(3)

        # Get user agent from zendriver page
        self._user_agent = await self._page.evaluate("navigator.userAgent")

        # Create TikTok-Api sessions using the shared zendriver browser.
        # Pass the existing page tab so no new tabs need to be opened
        # (new tabs steal OS-level window focus).
        await self.tiktok_api.create_sessions(
            zendriver_browser=self._zendriver_browser,
            num_sessions=self._num_sessions,
            starting_url='https://www.tiktok.com',
            existing_tab=self._page,
        )

        # TODO: test whether injecting visibility overrides into sessions helps
        # await self._inject_visibility_into_sessions()

        self._is_context_manager = True
        return self

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
        try:
            # Close TikTok-Api session tabs first (they live in the shared browser)
            await self.tiktok_api.close_sessions()
        except Exception:
            pass
        try:
            # Then stop the zendriver browser (which owns all tabs)
            zendriver_browser = getattr(self, "_zendriver_browser", None)
            if zendriver_browser:
                await zendriver_browser.stop()
        except Exception:
            pass

    async def __aexit__(self, type, value, traceback):
        await self.shutdown()

    async def refresh_sessions(self, refresh_zendriver: bool = True):
        """Reset TikTok-Api sessions to get fresh tokens/cookies.

        Call this when you notice API requests starting to fail consistently.
        This keeps the browser open but creates fresh TikTok-Api sessions
        (new tabs) with new device_id/odin_id and cookies.

        Args:
            refresh_zendriver: If True, also navigate the main page back to
                TikTok.com to refresh cookies. Defaults to True.
        """
        self.logger.info("Refreshing TikTok-Api sessions...")

        # Close existing TikTok-Api session tabs
        try:
            await self.tiktok_api.close_sessions()
        except Exception as e:
            self.logger.debug(f"Error closing sessions: {e}")

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

        # Recreate TikTok-Api sessions with fresh tokens
        await self.tiktok_api.create_sessions(
            zendriver_browser=self._zendriver_browser,
            num_sessions=self._num_sessions,
            starting_url='https://www.tiktok.com',
        )

        # TODO: test whether injecting visibility overrides into sessions helps
        # await self._inject_visibility_into_sessions()

        self.logger.info("Sessions refreshed successfully")

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
