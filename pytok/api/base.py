import asyncio
from collections import Counter
from datetime import datetime
import json
import random

from zendriver import cdp

from .. import exceptions, captcha_solver
from ..helpers import extract_tag_contents

TOK_DELAY = 20
CAPTCHA_DELAY = 999999

# --- infinite-scroll feeds ---------------------------------------------------
# How long one round waits for the listing response its scroll asked for. A healthy
# response lands in well under a second; this only bounds a slow or blocked one.
SCROLL_RESPONSE_TIMEOUT = 8
SCROLL_POLL_INTERVAL = 0.2
# How long to allow for the request itself to appear. Past this, a round in which the page
# has asked for nothing is not slow, it is stalled, and the remaining timeout would be spent
# waiting for a response nothing is going to send.
SCROLL_SILENT_GRACE = 1.5
# Floor per round, so a fast feed is paged at something like a human reading speed
# rather than as fast as the network allows.
SCROLL_MIN_ROUND = 0.6
SCROLL_ROUND_JITTER = 0.4
# A feed is only declared finished after this much loop-owned time with nothing new, and
# this many rounds. Both, because a page under load can take several rounds to answer, and
# giving up early is indistinguishable from reaching the end of a listing.
SCROLL_STALL_SECONDS = 25
SCROLL_STALL_ROUNDS = 5
# ...but rounds that come back fast (a feed re-sending a page we already have) would take
# far too many of them to fill the seconds above, so cap the rounds outright as well.
SCROLL_STALL_MAX_ROUNDS = 20

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

# TikTok's "Something went wrong / Refresh" panel, which stops a feed paginating until it
# is cleared. Shared between the scroll round below and check_and_resolve_refresh_button.
_CLICK_REFRESH_JS_BODY = """
  let refreshClicked = false;
  for (const el of document.querySelectorAll('p,button,div[role=button]')) {
    if (el.textContent && el.textContent.trim() === 'Refresh') {
      (el.closest('button') || el).click();
      refreshClicked = true;
      break;
    }
  }
"""

_CLICK_REFRESH_JS = "(() => {%s  return refreshClicked;\n})()" % _CLICK_REFRESH_JS_BODY

# One round trip that does everything a scroll round needs from the page: clear whatever
# TikTok has put over the feed, scroll the element that actually scrolls, and report where
# that got us.
#
# It deliberately goes through page.evaluate rather than zendriver's element API. select_all()
# fetches the whole DOM tree over CDP and then walks that tree once per match, so its cost
# grows with the square of the feed: on a grid of a thousand items a single 'is the Refresh
# button there?' check costs seconds, and it was charged once per round -- which capped how
# deep any walk could get before the loop was spending all its time on bookkeeping. The same
# work in JS is a millisecond at any feed size.
_SCROLL_FEED_JS = """
(() => {
  const itemSel = %(items)s;
  const containerSel = %(container)s;

  // Both of these sit over the feed and stop it paginating until dismissed.
%(refresh)s
  let loginClosed = false;
  const close = document.querySelector('[data-e2e="modal-close-inner-button"]');
  if (close) { close.click(); loginClosed = true; }

  // Some feeds scroll an inner container instead of the document, and there scrolling the
  // window is a no-op that never fires the pagination request. A caller that knows its
  // container says so; otherwise only go looking when the document does not scroll at all,
  // since a feed page has plenty of nested scrollable divs that are not the feed.
  const docEl = document.scrollingElement || document.documentElement;
  let el = containerSel ? document.querySelector(containerSel) : null;
  if (!el && itemSel && docEl.scrollHeight <= docEl.clientHeight + 4) {
    const item = document.querySelector(itemSel);
    for (let p = item && item.parentElement; p; p = p.parentElement) {
      const overflow = getComputedStyle(p).overflowY;
      if (p.scrollHeight > p.clientHeight + 4 && (overflow === 'auto' || overflow === 'scroll')) {
        el = p;
        break;
      }
    }
  }
  const scrollsDocument = !el || el.scrollHeight <= el.clientHeight;
  if (scrollsDocument) el = docEl;

  const before = el.scrollTop;
  if (scrollsDocument) {
    window.scrollBy(0, window.innerHeight * 3);
  } else {
    el.scrollTop = el.scrollHeight;
  }
  return {
    items: itemSel ? document.querySelectorAll(itemSel).length : 0,
    moved: el.scrollTop - before,
    atBottom: (el.scrollHeight - el.scrollTop - el.clientHeight) < 4,
    refreshClicked: refreshClicked,
    loginClosed: loginClosed,
    scrollsDocument: scrollsDocument,
  };
})()
"""

# Pull back up a viewport and a half. Once the feed is pinned to its bottom, scrolling
# down again is a no-op and the sentinel that triggers the next page cannot re-enter the
# viewport, so no amount of further scrolling will ever load anything: the only way to
# re-arm it is to leave and come back.
_NUDGE_FEED_JS = """
(() => {
  const containerSel = %(container)s;
  let el = containerSel ? document.querySelector(containerSel) : null;
  if (!el || el.scrollHeight <= el.clientHeight) el = document.scrollingElement || document.documentElement;
  el.scrollTop = Math.max(0, el.scrollTop - Math.round(el.clientHeight * 1.5));
  return el.scrollTop;
})()
"""


class ScrollRound:
    """One round of an infinite-scroll feed, handed to the caller to interpret.

    The caller sets `produced` to how many new items it got out of `responses`, since only
    it knows what its endpoint's payload looks like and what counts as new, and `stop` when
    the feed itself said the listing is over.
    """

    __slots__ = ('responses', 'state', 'requested', 'produced', 'stop')

    def __init__(self, responses, state, requested):
        self.responses = responses
        # what the page reported about the scroll itself (see _SCROLL_FEED_JS)
        self.state = state
        # whether the page asked for another page at all this round
        self.requested = requested
        self.produced = 0
        self.stop = False


class ScrollWalk:
    """Drives one infinite-scroll feed, and records how the walk went.

    Owns what every feed here shares: pacing, clearing the panels TikTok puts over a feed,
    waiting for the response a scroll asked for rather than sleeping a fixed interval,
    nudging a feed that has stopped asking for pages, and deciding when a feed that
    produces nothing is finished rather than merely slow.

    Callers add their own tallies to `stats` and put `summary()` in their log line, so one
    walk is one line saying how deep it got and which of the above happened.
    """

    def __init__(self, feed, url_pattern, item_selector=None, container_selector=None,
                 max_rounds=30, before_round=None):
        self.feed = feed
        self.url_pattern = url_pattern
        self.item_selector = item_selector
        self.container_selector = container_selector
        self.max_rounds = max_rounds
        # optional coroutine run at the top of each round (a captcha check, say)
        self.before_round = before_round

        self.stats = Counter()
        self.rounds_done = 0
        self.loop_seconds = 0.0
        self.feed_items = 0
        # Holds unless the walk ends on its own terms, which covers the caller closing the
        # generator -- a date window it has walked past, a session it has given up on. Do
        # not let that read as a budget we exhausted.
        self.reason = 'the caller stopped reading the walk'
        # True only when the feed itself said nothing follows. Every other way of stopping
        # is a walk that was cut off, which callers must not mistake for a complete one.
        self.listing_ended = False

    async def rounds(self):
        loop = asyncio.get_running_loop()
        stalled_for = 0.0
        stall_rounds = 0
        next_round_at = 0.0

        while self.rounds_done < self.max_rounds:
            now = loop.time()
            if now < next_round_at:
                await asyncio.sleep(next_round_at - now)

            round_started = loop.time()
            if self.before_round is not None:
                await self.before_round()

            state = await self.feed.scroll_feed(self.item_selector, self.container_selector)
            self.feed_items = state.get('items') or self.feed_items
            if state.get('refreshClicked'):
                self.stats['refresh_panels'] += 1
            if state.get('loginClosed'):
                self.stats['login_modals'] += 1

            responses, requested = await self.feed.await_api_responses(self.url_pattern)
            if not requested:
                self.stats['rounds_the_page_asked_for_nothing'] += 1

            # Timed before the caller sees the round, and the next round is scheduled from
            # this one's start: a caller that works on each item as it arrives (downloading
            # it, say) then pays the pacing out of time it was spending anyway, and cannot
            # spend the stall budget below on our behalf.
            round_seconds = loop.time() - round_started
            self.loop_seconds += round_seconds
            next_round_at = round_started + self.feed.scroll_round_floor()

            rnd = ScrollRound(responses, state, requested)
            yield rnd
            self.rounds_done += 1

            if rnd.stop:
                self.listing_ended = True
                self.reason = 'the feed said the listing ended'
                return

            if rnd.produced:
                stalled_for = 0.0
                stall_rounds = 0
                continue

            stall_rounds += 1
            stalled_for += round_seconds
            if not requested or state.get('atBottom') or not state.get('moved'):
                # pinned to the bottom, or not scrolling at all, so scrolling down again
                # cannot fire anything: back off so the next scroll can re-arm the feed
                if await self.feed.nudge_feed(self.container_selector):
                    self.stats['nudges'] += 1

            if stall_rounds >= SCROLL_STALL_MAX_ROUNDS or (
                    stall_rounds >= SCROLL_STALL_ROUNDS and stalled_for >= SCROLL_STALL_SECONDS):
                self.reason = f'nothing new for {stalled_for:.0f}s over {stall_rounds} rounds'
                return

        self.reason = f'ran out of scrolls ({self.max_rounds})'

    def summary(self):
        parts = [f"{self.rounds_done} scrolls in {self.loop_seconds:.0f}s"]
        if self.item_selector:
            parts.append(f"feed held {self.feed_items} items")
        parts += [f"{name.replace('_', ' ')} {n}" for name, n in sorted(self.stats.items()) if n]
        return ", ".join(parts) + f"; stopped: {self.reason}"


class Base:

    def scroll_walk(self, url_pattern, item_selector=None, container_selector=None,
                    max_rounds=30, before_round=None):
        """A ScrollWalk over this feed — see that class."""
        return ScrollWalk(self, url_pattern, item_selector=item_selector,
                          container_selector=container_selector, max_rounds=max_rounds,
                          before_round=before_round)

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

    async def _wait_for_page_load(self, page, what, required=True):
        """Wait for the current navigation to reach readyState 'complete'.

        Honours PyTok's page_load_timeout rather than imposing a ceiling of its own.

        `required=False` downgrades a timeout to a warning, for callers that follow this
        with a stronger readiness gate of their own (waiting for the content grid, say).
        'complete' waits on every image, video and beacon the page pulls in, so on a
        heavy profile it lands tens of seconds after the content is usable, or not at
        all -- failing there would throw away a page the caller could have scraped.

        When it does raise, it raises pytok's TimeoutException rather than letting the
        bare TimeoutError out. That one stringifies to '', which tells a log reader
        nothing, and being an unrecognised type it lands in the accounts pool's generic
        handler, which rebuilds the session in place on the same account -- no use when
        the page is merely slow, and it repeats for every handle.
        """
        timeout = getattr(self.parent, '_page_load_timeout', 45)
        try:
            async with asyncio.timeout(timeout):
                await page.wait_for_ready_state(until='complete', timeout=timeout + 1)
        except (asyncio.TimeoutError, TimeoutError) as ex:
            msg = f"{what} did not reach readyState 'complete' within {timeout}s"
            if required:
                raise exceptions.TimeoutException(msg) from ex
            self.parent.logger.warning(f"{msg}; carrying on to wait for content")

    async def _wait_for_page_data(self, page, what, fallback=None):
        """Wait until the page's data is readable.

        Usually that means the embedded data tag. It lands early in the load, whereas
        readyState 'complete' can be tens of seconds later on a heavy page -- so gating
        on 'complete' fails pages whose data has been sitting in the document the whole
        time. Polls rather than sleeping a fixed interval, because for the first moments
        after a navigation get_content() returns a half-built document with no tag in it.

        `fallback` is an optional coroutine for callers whose data can arrive by another
        route: TikTok also serves these pages client-rendered, as a shell whose script
        fetches the data over XHR and leaves nothing embedded to parse. It is polled
        alongside the tag, and ending the wait on whichever appears first means a
        client-rendered page costs no more than a server-rendered one.
        """
        timeout = getattr(self.parent, '_page_load_timeout', 45)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            # A response body only lives in Chrome until it decides to drop it, so bank
            # them as they finish rather than at the end of the wait: the listing the page
            # fetches on load is often the only copy there is, and this wait can run for
            # tens of seconds beside it.
            await self.parent.collect_pending_response_bodies()
            if fallback is not None and await fallback():
                return
            try:
                html = await page.get_content()
                if html and extract_tag_contents(html):
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)

        raise exceptions.TimeoutException(
            f"{what}: no page data appeared within {timeout}s"
        )

    async def _is_captcha_visible(self):
        """Whether any captcha's text is on the page.

        One page.evaluate over the rendered text rather than a DOM search per string.
        zendriver's find() fetches and walks the document once per call, so on a feed that
        has scrolled to a few hundred items this check cost seconds -- and a scroll walk
        charges it to every round. Falls back to the per-string search if the page will not
        evaluate, which is itself a state worth not guessing about.
        """
        js = ("(() => { const t = document.body ? document.body.innerText : ''; "
              "return %s.some(s => t.includes(s)); })()" % json.dumps(CAPTCHA_TEXTS))
        try:
            return bool(await self.parent._page.evaluate(js))
        except Exception:
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
        """Click TikTok's 'Refresh' panel if it is up.

        In one page.evaluate rather than through select_all('p'), which fetches the whole
        DOM tree and then walks it once per match -- fine on a freshly loaded page, seconds
        on a feed that has been scrolled (see _SCROLL_FEED_JS).
        """
        self.parent.logger.debug("Checking for refresh button")
        try:
            clicked = await self.parent._page.evaluate(_CLICK_REFRESH_JS)
        except Exception:
            return False
        if clicked:
            self.parent.logger.debug("Refresh button found, clicking")
            await asyncio.sleep(1)
        return bool(clicked)

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


    async def scroll_feed(self, item_selector=None, container_selector=None):
        """Advance an infinite-scroll feed by one round.

        Returns the page's own account of what happened: how many items the feed now
        holds, whether the scroll moved anything, whether we are pinned to the bottom,
        and whether a Refresh panel or login modal had to be cleared. `atBottom` and
        `moved` are what let a caller tell 'the feed is slow' from 'the feed cannot
        load any more without being nudged' -- see _NUDGE_FEED_JS.
        """
        js = _SCROLL_FEED_JS % {
            'items': json.dumps(item_selector) if item_selector else 'null',
            'container': json.dumps(container_selector) if container_selector else 'null',
            'refresh': _CLICK_REFRESH_JS_BODY,
        }
        try:
            state = await self.parent._page.evaluate(js)
        except Exception as ex:
            self.parent.logger.debug(f"Scroll round failed: {ex}")
            return {}
        return state if isinstance(state, dict) else {}

    async def nudge_feed(self, container_selector=None):
        """Scroll back up so the next scroll down can re-trigger pagination."""
        js = _NUDGE_FEED_JS % {
            'container': json.dumps(container_selector) if container_selector else 'null',
        }
        try:
            await self.parent._page.evaluate(js)
            return True
        except Exception as ex:
            self.parent.logger.debug(f"Feed nudge failed: {ex}")
            return False

    async def await_api_responses(self, url_pattern, timeout=SCROLL_RESPONSE_TIMEOUT,
                                  poll=SCROLL_POLL_INTERVAL):
        """Wait for the listing responses a scroll asked for, returning as soon as they land.

        Returns (responses, requested). `requested` records whether the page had such a
        request in flight (or already collected) at any point during the wait, which is
        the difference between a response that is merely slow and a feed whose own
        pagination never fired -- the two need opposite remedies, and a flat sleep
        cannot tell them apart.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + timeout
        requested = False
        while True:
            # before process_pending_responses, which consumes what it matches
            requested = requested or bool(self.parent.seen_request_urls(url_pattern))
            responses = await self.parent.process_pending_responses(url_pattern)
            if responses:
                return responses, True
            now = loop.time()
            if now >= deadline:
                return [], requested
            if not requested and now >= started + SCROLL_SILENT_GRACE:
                # the page has not asked for anything, so no response is on its way and
                # the rest of the timeout would be spent waiting for one that never comes
                return [], False
            await asyncio.sleep(poll)

    def scroll_round_floor(self):
        """Minimum spacing between the starts of two scroll rounds, jittered.

        Measured from the start of a round rather than slept at the end of one, so a
        caller that spends time on each video (downloading it, say) pays this out of
        time it was going to spend anyway instead of on top of it.
        """
        return SCROLL_MIN_ROUND + random.uniform(0, SCROLL_ROUND_JITTER)

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

