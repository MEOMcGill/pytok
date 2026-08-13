import asyncio
from datetime import datetime
import json
import random

from zendriver import cdp

from .. import exceptions, captcha_solver

TOK_DELAY = 20
CAPTCHA_DELAY = 999999

# Text patterns for detecting various page states
CAPTCHA_TEXTS = [
    'Rotate the shapes',
    'Verify to continue:',
    'Click on the shapes with the same size',
    'Drag the slider to fit the puzzle'
]

LOGIN_CLOSE_TEXTS = [
    "Continue as guest",
    "Continue without login"
]


class Base:

    async def _find_element_by_selector(self, selector, timeout=5):
        """Find element by CSS selector, returns None if not found."""
        page = self.parent._page
        try:
            element = await page.select(selector, timeout=timeout)
            return element
        except Exception:
            return None

    async def _find_element_by_text(self, text, timeout=5):
        """Find element containing text, returns None if not found."""
        page = self.parent._page
        try:
            element = await page.find(text, timeout=timeout)
            return element
        except Exception:
            return None

    async def _is_text_visible(self, text):
        """Check if text is visible on the page."""
        page = self.parent._page
        try:
            element = await page.find(text, timeout=1)
            return element is not None
        except Exception:
            return False

    async def _find_p_element_by_text(self, text, timeout=5):
        """Find a p element containing the specified text, returns None if not found."""
        page = self.parent._page
        try:
            p_elements = await page.select_all('p', timeout=timeout)
            for p in p_elements:
                if hasattr(p, 'text') and p.text and text in p.text:
                    return p
            return None
        except Exception:
            return None

    async def _is_selector_visible(self, selector):
        """Check if selector is visible on the page."""
        try:
            element = await self._find_element_by_selector(selector, timeout=1)
            return element is not None
        except Exception:
            return False

    async def _wait_for_page_load(self, page, what):
        """Wait for the current navigation to reach readyState 'complete'.

        Honours PyTok's page_load_timeout rather than imposing a ceiling of its own: a
        profile page reaches 'interactive' seconds before 'complete', so a ceiling
        shorter than the real load time throws away pages whose embedded data tag is
        already there and parseable.

        Raises pytok's TimeoutException rather than letting the bare TimeoutError out.
        That one stringifies to '', which tells a log reader nothing, and being an
        unrecognised type it lands in the accounts pool's generic handler, which
        rebuilds the session in place on the same account -- no use when the page is
        merely slow, and it repeats for every handle.
        """
        timeout = getattr(self.parent, '_page_load_timeout', 45)
        try:
            async with asyncio.timeout(timeout):
                await page.wait_for_ready_state(until='complete', timeout=timeout + 1)
        except (asyncio.TimeoutError, TimeoutError) as ex:
            raise exceptions.TimeoutException(
                f"{what} did not reach readyState 'complete' within {timeout}s"
            ) from ex

    async def _is_captcha_visible(self):
        """Check if any captcha text is visible."""
        for text in CAPTCHA_TEXTS:
            if await self._is_text_visible(text):
                return True
        return False

    async def check_initial_call(self, url):
        # For zendriver, we check responses via CDP - wait a bit for navigation
        await asyncio.sleep(2)
        responses = await self.parent.process_pending_responses(url)
        for resp in responses:
            status = resp.get('response', {})
            if hasattr(status, 'status') and status.status >= 300:
                raise exceptions.NotAvailableException("Content is not available")

    async def wait_for_content_or_captcha(self, content_tag):
        page = self.parent._page

        max_tries = 10
        tries = 0
        self.parent.logger.debug("Waiting for main content to become visible")
        while tries < max_tries:
            is_content_visible = await self._is_selector_visible(content_tag)
            is_captcha_visible = await self._is_captcha_visible()
            if is_content_visible or is_captcha_visible:
                break
            await asyncio.sleep(0.5)
            await self.check_and_resolve_refresh_button()
            tries += 1

        if await self._is_captcha_visible():
            await self.solve_captcha()
            await asyncio.sleep(1)
            # Wait for content after captcha
            for _ in range(TOK_DELAY * 2):
                if await self._is_selector_visible(content_tag):
                    break
                await asyncio.sleep(0.5)

        return await self._find_element_by_selector(content_tag, timeout=1)

    async def wait_for_content_or_unavailable(self, content_tag, unavailable_text, no_content_text=None):
        if await self._find_element_by_selector(content_tag):
            return await self._find_element_by_selector(content_tag, timeout=1)

        page = self.parent._page

        await self.check_and_resolve_refresh_button()
        await self.check_and_resolve_login_popup()

        self.parent.logger.debug(f"Checking for '{unavailable_text}'")
        if await self._is_text_visible(unavailable_text):
            raise exceptions.NotAvailableException(f"Content is not available with message: '{unavailable_text}'")

        if no_content_text:
            texts = no_content_text if isinstance(no_content_text, list) else [no_content_text]
            for text in texts:
                if await self._is_text_visible(text):
                    raise exceptions.NoContentException(f"Content is not available with message: '{text}'")
                else:
                    self.parent.logger.debug(f"Could not find text '{text}'")

        max_tries = 10
        tries = 0
        self.parent.logger.debug("Waiting for main content to become visible")
        while not (await self._is_selector_visible(content_tag)) and tries < max_tries:
            await asyncio.sleep(0.5)
            await self.check_and_resolve_refresh_button()
            tries += 1

        if tries >= max_tries:
            # try some other behaviour
            current_url = page.url
            # Bounce off the page only after banking what it already fetched:
            # response bodies are read lazily and their request ids die with the
            # page (see PyTok.collect_pending_response_bodies).
            await self.parent.collect_pending_response_bodies()
            await page.get("https://www.tiktok.com")
            await asyncio.sleep(5)
            await page.get(current_url)

        return await self._find_element_by_selector(content_tag, timeout=1)

    async def check_and_resolve_refresh_button(self):
        page = self.parent._page
        self.parent.logger.debug("Checking for refresh button")
        try:
            refresh_button = await self._find_p_element_by_text('Refresh', timeout=1)
            if refresh_button:
                self.parent.logger.debug("Refresh button found, clicking")
                await refresh_button.click()
                await asyncio.sleep(1)
        except Exception:
            pass

    async def check_and_resolve_login_popup(self):
        page = self.parent._page
        self.parent.logger.debug("Checking for login to TikTok pop up")
        try:
            login_popup = await self._find_p_element_by_text('Log in to TikTok', timeout=1)
            if login_popup:
                self.parent.logger.debug("Login prompt found, checking for close button")
                # Try multiple selectors for the close button
                close_selectors = [
                    '[data-e2e="modal-close-inner-button"]',
                    '[class*="close"]',
                    'button[aria-label="Close"]',
                    'svg[class*="close"]',
                ]
                closed = False
                for selector in close_selectors:
                    try:
                        login_close = await self._find_element_by_selector(selector, timeout=1)
                        if login_close:
                            await login_close.click()
                            await asyncio.sleep(1)
                            closed = True
                            self.parent.logger.debug(f"Closed login popup with selector: {selector}")
                            break
                    except Exception:
                        continue

                if not closed:
                    # Try pressing Escape key to close the modal
                    try:
                        await page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
                        await asyncio.sleep(1)
                        self.parent.logger.debug("Closed login popup with Escape key")
                    except Exception:
                        # If we can't close it, just continue - the user might still be able to use the page
                        self.parent.logger.debug("Could not close login popup, continuing anyway")
        except Exception as e:
            self.parent.logger.debug(f"Error checking login popup: {e}")


    async def wait_for_content_or_unavailable_or_captcha(self, content_tag, unavailable_text, no_content_text=None):
        if await self._is_selector_visible(content_tag):
            return await self._find_element_by_selector(content_tag, timeout=1)

        await self.check_and_resolve_refresh_button()
        await self.check_and_resolve_login_popup()

        self.parent.logger.debug("Checking for captcha")
        if await self._is_captcha_visible():
            self.parent.logger.debug("Captcha found")
            await self.solve_captcha()
            await asyncio.sleep(1)
            if await self._is_captcha_visible():
                raise exceptions.CaptchaException("Captcha is still visible after solving")
            # Wait for content or unavailable after captcha
            for _ in range(TOK_DELAY * 2):
                if await self._is_selector_visible(content_tag):
                    break
                if await self._is_text_visible(unavailable_text):
                    break
                await asyncio.sleep(0.5)

        # check after resolving captcha
        await self.check_and_resolve_refresh_button()
        await self.check_and_resolve_login_popup()

        self.parent.logger.debug(f"Checking for '{unavailable_text}'")
        if await self._find_p_element_by_text(unavailable_text):
            raise exceptions.NotAvailableException(f"Content is not available with message: '{unavailable_text}'")

        if no_content_text:
            texts = no_content_text if isinstance(no_content_text, list) else [no_content_text]
            for text in texts:
                if await self._find_p_element_by_text(text):
                    raise exceptions.NoContentException(f"Content is not available with message: '{text}'")
                else:
                    self.parent.logger.debug(f"Could not find text '{text}'")

        max_tries = 3
        tries = 0
        self.parent.logger.debug("Waiting for main content to become visible")
        content_is_visible = await self._is_selector_visible(content_tag)
        if content_is_visible:
            return
        
        while not content_is_visible and tries < max_tries:
            await asyncio.sleep(1)
            await self.check_and_resolve_refresh_button()
            tries += 1
            content_is_visible = await self._is_selector_visible(content_tag)
            if content_is_visible:
                return
            
        # now try some other behaviour
        page = self.parent._page
        current_url = page.url
        # Bank the bodies of anything this page already fetched before leaving it:
        # their request ids stop resolving once the page is gone (see
        # PyTok.collect_pending_response_bodies).
        await self.parent.collect_pending_response_bodies()
        await page.send(cdp.page.navigate("https://www.tiktok.com"))
        await asyncio.sleep(3)

        # do some scrolling
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            await asyncio.sleep(2)

        await page.send(cdp.page.navigate(current_url))

        # Poll for the content instead of reading once after a fixed sleep. A page's
        # embedded data tag and its content grid land seconds apart, and for the first
        # few seconds get_content() returns a half-built document with no tag at all --
        # so a caller that returns here on a short sleep hands back a page whose data
        # the caller then fails to parse, reported as if the page had no data.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + getattr(self.parent, '_page_load_timeout', 45)
        while loop.time() < deadline:
            if await self._is_selector_visible(content_tag):
                return await self._find_element_by_selector(content_tag, timeout=1)
            await asyncio.sleep(1)

        raise exceptions.TimeoutException("Content did not become visible in time")

    async def check_for_unavailable_or_captcha(self, unavailable_text):
        page = self.parent._page

        captcha_visible = await self._is_captcha_visible()
        if captcha_visible:
            num_tries = 0
            max_tries = 3
            captcha_exceptions = []
            while num_tries < max_tries:
                num_tries += 1
                try:
                    await self.solve_captcha()
                    await asyncio.sleep(1)
                    captcha_is_visible = await self._is_captcha_visible()
                    if captcha_is_visible:
                        captcha_exceptions.append(exceptions.CaptchaException("Captcha is still visible after solving"))
                        continue
                    else:
                        break
                except Exception as e:
                    captcha_exceptions.append(e)
            else:
                print(
                    f"Failed to solve captcha after {max_tries} tries with errors: {captcha_exceptions}, continuing anyway...")

        # Check for login close buttons
        for login_text in LOGIN_CLOSE_TEXTS:
            try:
                login_element = await page.find(login_text, timeout=1)
                if login_element:
                    await login_element.click()
                    break
            except Exception as e:
                print(f"Failed to close login with error: {e}, continuing anyway...")

        if await self._is_text_visible(unavailable_text):
            raise exceptions.NotAvailableException(f"Content is not available with message: '{unavailable_text}'")

    async def check_for_unavailable(self, unavailable_text):
        if await self._is_text_visible(unavailable_text):
            raise exceptions.NotAvailableException(f"Content is not available with message: '{unavailable_text}'")

    async def check_for_reload_button(self):
        try:
            reload_button = await self._find_element_by_text('Refresh', timeout=1)
            if reload_button:
                await reload_button.click()
        except Exception:
            pass

    async def wait_for_requests(self, api_path, timeout=TOK_DELAY):
        # With zendriver, we use CDP events - wait and process
        for _ in range(timeout * 2):
            responses = await self.parent.process_pending_responses(api_path)
            if responses:
                return responses[0]
            await asyncio.sleep(0.5)
        raise exceptions.TimeoutException(f"Timeout waiting for request: {api_path}")

    def get_requests(self, api_path):
        """Get pending requests matching the API path from CDP tracking."""
        return [
            info for info in self.parent._pending_requests.values()
            if api_path in info.get('url', '')
        ]

    def get_responses(self, api_path):
        """Get collected responses matching the API path."""
        return [
            resp for resp in self.parent._collected_responses
            if api_path in resp.get('url', '')
        ]

    async def get_response_body(self, response):
        """Get the body from a response dict."""
        if isinstance(response, dict):
            return response.get('body', b'')
        return b''

    async def scroll_to_bottom(self, speed=4):
        page = self.parent._page
        current_scroll_position = await page.evaluate(
            "document.documentElement.scrollTop || document.body.scrollTop")
        new_height = current_scroll_position + 1
        while current_scroll_position <= new_height:
            current_scroll_position += speed + random.randint(-speed, speed)
            await page.evaluate(f"window.scrollTo(0, {current_scroll_position})")
            new_height = await page.evaluate("document.body.scrollHeight")

    async def scroll_to(self, position, speed=5):
        page = self.parent._page
        current_scroll_position = await page.evaluate(
            "document.documentElement.scrollTop || document.body.scrollTop")
        new_height = current_scroll_position + 1
        while current_scroll_position <= new_height:
            current_scroll_position += speed + random.randint(-speed, speed)
            await page.evaluate(f"window.scrollTo(0, {current_scroll_position})")
            new_height = await page.evaluate("document.body.scrollHeight")
            if current_scroll_position > position:
                break

    async def slight_scroll_up(self, speed=4):
        page = self.parent._page
        desired_scroll = -500
        current_scroll = 0
        while current_scroll > desired_scroll:
            current_scroll -= speed + random.randint(-speed, speed)
            await page.evaluate(f"window.scrollBy(0, {-speed})")

    async def scroll_down(self, amount, speed=4):
        page = self.parent._page

        current_scroll_position = await page.evaluate(
            "document.documentElement.scrollTop || document.body.scrollTop")
        desired_position = current_scroll_position + amount
        while current_scroll_position < desired_position:
            scroll_amount = speed + random.randint(-speed, speed) * 0.5
            await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            new_scroll_position = await page.evaluate(
                "document.documentElement.scrollTop || document.body.scrollTop")
            if new_scroll_position > current_scroll_position:
                current_scroll_position = new_scroll_position
            else:
                # we hit the bottom
                break

    async def wait_until_not_skeleton_or_captcha(self, skeleton_tag):
        page = self.parent._page
        selector = f'[data-e2e={skeleton_tag}]'
        # Wait for skeleton to disappear
        for _ in range(TOK_DELAY * 2):
            if not await self._is_selector_visible(selector):
                return
            await asyncio.sleep(0.5)

        # Check if captcha appeared
        if await self._is_captcha_visible():
            await self.solve_captcha()
            await asyncio.sleep(1)
        else:
            raise exceptions.TimeoutException(f"Skeleton element still visible: {skeleton_tag}")

    async def check_and_wait_for_captcha(self):
        if await self._is_captcha_visible():
            await self.solve_captcha()
            await asyncio.sleep(1)

    async def check_and_close_signin(self):
        page = self.parent._page
        for login_text in LOGIN_CLOSE_TEXTS:
            try:
                signin_element = await page.find(login_text, timeout=1)
                if signin_element:
                    await signin_element.click()
                    return
            except Exception:
                pass

    async def solve_captcha(self):
        if self.parent._manual_captcha_solves:
            input("Press Enter to continue after solving CAPTCHA:")
            await asyncio.sleep(1)
            if self.parent._log_captcha_solves:
                requests = self.get_requests('/captcha/verify')
                if requests:
                    body = requests[0].get('body', '')
                    with open(f"manual_captcha_{datetime.now().isoformat()}.json", "w") as f:
                        f.write(body)
            return

        # Get captcha data from CDP responses
        captcha_responses = await self.parent.process_pending_responses('/captcha/get')
        if not captcha_responses:
            raise exceptions.EmptyResponseException("No captcha response found")

        captcha_body = captcha_responses[0].get('body', '')
        if not captcha_body:
            raise exceptions.EmptyResponseException("Empty captcha response body")

        captcha_json = json.loads(captcha_body)

        if 'mode' in captcha_json['data']:
            captcha_data = captcha_json['data']
        elif 'challenges' in captcha_json['data']:
            captcha_data = captcha_json['data']['challenges'][0]
        else:
            raise exceptions.CaptchaException("Unknown captcha data format")

        captcha_type = captcha_data['mode']
        if captcha_type not in ['slide', 'whirl']:
            raise exceptions.CaptchaException(f"Unsupported captcha type: {captcha_type}")

        # Get puzzle image from CDP responses
        puzzle_url = captcha_data['question']['url1']
        puzzle_responses = await self.parent.process_pending_responses(puzzle_url)
        if not puzzle_responses:
            raise exceptions.CaptchaException("Puzzle was not found in response")
        puzzle = puzzle_responses[0].get('body', b'')
        if isinstance(puzzle, str):
            puzzle = puzzle.encode()

        if not puzzle:
            raise exceptions.CaptchaException("Puzzle was not found in response")

        # Get puzzle piece image from CDP responses
        piece_url = captcha_data['question']['url2']
        piece_responses = await self.parent.process_pending_responses(piece_url)
        if not piece_responses:
            raise exceptions.CaptchaException("Piece was not found in response")
        piece = piece_responses[0].get('body', b'')
        if isinstance(piece, str):
            piece = piece.encode()

        if not piece:
            raise exceptions.CaptchaException("Piece was not found in response")

        # Solve captcha using the solver
        page = self.parent._page
        # Create a response-like object for the captcha solver
        captcha_response_obj = type('Response', (), {'json': lambda: captcha_json})()
        solver = captcha_solver.CaptchaSolver(captcha_response_obj, puzzle, piece, page=page)
        await solver.solve_and_drag()

        if self.parent._log_captcha_solves:
            await asyncio.sleep(1)
            verify_responses = await self.parent.process_pending_responses('/captcha/verify')
            if verify_responses:
                body = verify_responses[0].get('body', '')
                with open(f"automated_captcha_{datetime.now().isoformat()}.json", "w") as f:
                    f.write(body)

