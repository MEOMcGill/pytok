from __future__ import annotations

import asyncio
import json
import math
import urllib.parse
from typing import TYPE_CHECKING, Iterator, Optional
from urllib.parse import parse_qs, urlparse

from zendriver import cdp

from .user import User
from .video import Video
from .base import Base
from ..exceptions import *

if TYPE_CHECKING:
    from ..tiktok import PyTok

# web_search_code mirrors what the TikTok web app sends with search requests
_WEB_SEARCH_CODE = '{"tiktok":{"client_params_x":{"search_engine":{"ies_mt_user_live_video_card_use_libra":1,"mt_search_general_user_live_card":1}},"search_server":{}}}'

# TikTok's search results render inside an inner scroll container, not the document
# body -- so scrolling the window does nothing and never fires the lazily-loaded
# pagination requests. base.scroll_feed falls back to the window if this is not
# found, in case the layout changes.
_SEARCH_GRID_SELECTOR = '#grid-main, [class*=SearchGridLayoutContainer]'

# Scrolls with no `count` to size them are only ended by the walk's stall check; this is
# just a backstop against a page that never stops producing.
_UNBOUNDED_MAX_ROUNDS = 400

# Page size TikTok serves videos at. User search pages come 20 at a time, so
# this is only used to size scroll budgets — as the smaller of the two it can
# overshoot but never leave a request short. Anything that has to reason about
# what a full page looks like uses Search._note_page_size instead.
_VIDEO_PAGE_SIZE = 12


def _endpoint_path(obj_type):
    """The path of the result-page endpoint for this object type.

    The search page also fires api/search/* requests that are not result pages
    (suggest/guide, general/preview, ...). Their payloads carry no has_more, so a
    filter loose enough to match them reads as "no more results" and stops
    pagination dead — always filter on the full path for the object type.
    """
    return f"api/search/{obj_type}/full"


class Search(Base):
    """Contains methods for searching TikTok."""

    parent: PyTok
    """The PyTok instance this object is bound to (set by api.search(...))."""

    _exhausted_listing = False
    """Whether the listing was last read all the way to its end. See
    _note_page_size. Reset per search_type() call."""
    _max_page_size = 0
    """Largest page of results this search has been served. See
    _note_page_size. Reset per search_type() call."""

    def __init__(self, search_term, parent: Optional[PyTok] = None):
        self.parent = parent
        self.search_term = search_term

    async def videos(self, count=28, offset=0, **kwargs) -> Iterator[Video]:
        """
        Searches for Videos

        - Parameters:
            - count (int | None): The amount of videos you want returned.
              None means every result in the listing.
            - offset (int): The offset of videos from your data you want returned.
            - prefer_scraping (bool): If True, get results by scrolling the browser
              page rather than through the make_request API. See search_type().

        Example Usage
        ```py
        async for video in api.search('therock').videos():
            # do something
        ```
        """
        async for result in self.search_type("item", count=count, offset=offset, **kwargs):
            yield result

    async def users(self, count=28, offset=0, **kwargs) -> Iterator[User]:
        """
        Searches for users.

        - Parameters:
            - count (int | None): The amount of users you want returned.
              None means every result in the listing.
            - offset (int): The offset of users from your data you want returned.
            - prefer_scraping (bool): If True, get results by scrolling the browser
              page rather than through the make_request API. See search_type().

        Example Usage
        ```py
        async for user in api.search('therock').users():
            # do something
        ```
        """
        async for result in self.search_type("user", count=count, offset=offset, **kwargs):
            yield result

    async def search_type(self, obj_type, count=28, offset=0, prefer_scraping=False, **kwargs) -> Iterator:
        """
        Searches for a specific type of object. Use .videos() & .users() instead.

        - Parameters:
            - count (int | None): The amount of objects you want returned.
              None means keep paginating until the listing runs out.
            - offset (int): The offset of objects you want returned.
            - obj_type (str): user | item
            - prefer_scraping (bool): If True, skip the make_request API route and
              get results by scrolling the browser page.

        Results come from up to three routes in turn, each picking up the cursor
        the last one reached, so a route that stalls costs pages rather than the
        whole search: the API (replaying the param template captured from the
        webapp's own search requests, changing only keyword, cursor and
        search_id), the search page's own responses, then scrolling that page.
        Unlike the other endpoints, TikTok currently answers signed search
        requests it did not issue itself with an empty body — even a byte-for-byte
        template replay — so in practice the page routes do the work here and the
        API route is an opportunistic fast path for if that changes.
        """
        if obj_type not in ("user", "item"):
            raise TypeError("invalid obj_type")

        # Set by whichever route last read the end of the listing, so we only
        # stop early on TikTok's own word that there is nothing left. See
        # _note_page_size for what counts as its word.
        self._exhausted_listing = False
        self._max_page_size = 0

        seen_ids = set()
        amount_yielded = 0

        # count=None means "keep going until the listing runs out", so every stop
        # check has to ask whether there is a limit at all before comparing to it.
        def enough():
            return count is not None and amount_yielded >= count

        wanted = count if count is not None else "all"

        async def emit(source):
            """Yield results from a source, deduping and honouring `count`."""
            nonlocal amount_yielded
            async for result, result_id in source:
                if result_id is not None:
                    if result_id in seen_ids:
                        continue
                    seen_ids.add(result_id)
                amount_yielded += 1
                yield result
                if enough():
                    return

        if not prefer_scraping:
            try:
                async for result in emit(self._search_type_api(obj_type, cursor=offset)):
                    yield result
                if enough() or (amount_yielded and self._exhausted_listing):
                    return
                self.parent.logger.warning(
                    f"API path returned {amount_yielded} search results ({obj_type}) "
                    f"of {wanted} and stopped short of the end of the listing. "
                    "Falling back to scraping method."
                )
            except ApiFailedException as ex:
                self.parent.logger.warning(
                    f"TikTok-Api search ({obj_type}) failed: {ex}. Falling back to scraping method."
                )

        # Scraping route. Loading the search page fires the webapp's own search
        # request, which fills the param template for that endpoint
        # (PyTok._on_request_will_be_sent -> cache_api_params). So we harvest that
        # first page off the wire and then resume paginating through the API from
        # its cursor and search_id, instead of scrolling for every page.
        await self._load_search_page(obj_type)
        results, has_more, cursor, search_id = await self._harvest_page_results(obj_type)
        self.parent.logger.info(
            f"Got {len(results)} search results from the search page, "
            f"has_more={has_more}, cursor={cursor}"
        )
        async for result in emit(self._aiter(results)):
            yield result
        if enough():
            return
        if not has_more:
            return

        if not prefer_scraping:
            try:
                async for result in emit(
                    self._search_type_api(obj_type, cursor=cursor, search_id=search_id)
                ):
                    yield result
                if enough() or self._exhausted_listing:
                    return
                # The resumed API stopped short of the end of the listing. Don't
                # settle for what we have — carry on by scrolling, which
                # paginates the same listing without replaying any params.
                self.parent.logger.warning(
                    f"Resumed API path stopped at {amount_yielded} search results "
                    f"of {wanted}, short of the end of the listing. "
                    "Continuing by scrolling the page."
                )
            except ApiFailedException as ex:
                self.parent.logger.warning(
                    f"API still failing after the search page load ({ex}). "
                    "Continuing by scrolling the page."
                )

        remaining = None if count is None else count - amount_yielded
        async for result in emit(self._scroll_for_results(obj_type, count=remaining)):
            yield result

    def _note_page_size(self, page_size):
        """Record a page of results, and say whether it was a short one.

        A listing that has genuinely run out ends on a short page — every full
        page before it comes with has_more=1. So has_more=0 landing on an exactly
        full page is not the end of the listing, it's what a throttled or
        rejected request looks like, and is worth another route rather than
        ending the search. A full page is whatever the biggest page seen so far
        was (videos come 12 at a time, users 20) rather than an assumed size, so
        every page has to be passed through here — including, and especially, the
        full ones that calibrate it.
        """
        if page_size <= 0:
            return False
        self._max_page_size = max(self._max_page_size, page_size)
        return page_size < self._max_page_size

    @staticmethod
    async def _aiter(results):
        for result in results:
            yield result

    async def _search_type_api(self, obj_type, cursor=0, search_id="", **kwargs) -> Iterator:
        # See _exhausted_listing: only this route's own last page can set it.
        self._exhausted_listing = False
        # TikTok ties all pages of one search to a search_id: the logid of the
        # FIRST response, echoed back unchanged on every later page. Without it
        # the server returns an empty item_list with has_more=0, capping results
        # at 12. When we resume after a page load we reuse the search_id of the
        # page's own requests, so the API continues that same search rather than
        # starting a fresh one.
        while True:
            params = {
                "keyword": self.search_term,
                "cursor": cursor,
                "offset": cursor,
                "from_page": "search",
                "search_id": search_id,
                "web_search_code": _WEB_SEARCH_CODE,
            }

            try:
                res = await self.parent.tiktok_api.make_request(
                    url=f"https://www.tiktok.com/api/search/{obj_type}/full/",
                    params=params,
                )
            except Exception as e:
                raise ApiFailedException(f"TikTok-Api make_request failed: {e}")

            if res is None:
                raise ApiFailedException("TikTok-Api returned None response")

            if res.get('type') == 'verify':
                raise ApiFailedException("TikTok API is asking for verification")

            if not search_id:
                search_id = (res.get("extra") or {}).get("logid", "") or search_id

            page_size = 0
            for result in self._yield_results(obj_type, res, with_id=True):
                page_size += 1
                yield result
            short_page = self._note_page_size(page_size)

            if not res.get("has_more", 0):
                if page_size == 0:
                    # A nil response: an empty listing with has_more=0. TikTok
                    # sends this when it rejects the replayed request (stale
                    # search_id, burned template, rate limiting) as well as at a
                    # genuine end of results, and the two are indistinguishable
                    # from here. Report failure so the caller can fall back to
                    # the page instead of ending the search on a maybe.
                    raise ApiFailedException(
                        f"TikTok returned an empty search page at cursor {cursor}"
                    )
                self._exhausted_listing = short_page
                self.parent.logger.info(
                    f"TikTok is not sending results beyond cursor {cursor} "
                    f"(last page held {page_size})."
                )
                return

            # Never re-request the same cursor: a response that repeats (or
            # omits) the cursor would otherwise loop forever on one page, with
            # dedup hiding it as a stall rather than an error.
            next_cursor = res.get("cursor", cursor)
            if not isinstance(next_cursor, int) or next_cursor <= cursor:
                next_cursor = cursor + page_size
            cursor = next_cursor
            await self.parent.request_delay()

    async def _load_search_page(self, obj_type):
        """Navigate to the search results page so its search request fires."""
        page = self.parent._page

        # Drop anything captured for earlier operations first, so what we harvest
        # after the navigation belongs to this search.
        await self.parent.process_pending_responses()

        subpath = "user" if obj_type == "user" else "video"
        url = f"https://www.tiktok.com/search/{subpath}?q={urllib.parse.quote(self.search_term)}"
        self.parent.logger.debug(f"Loading page: {url}")
        await page.send(cdp.page.navigate(url))
        async with asyncio.timeout(30):
            await page.wait_for_ready_state(until='complete', timeout=31)
        await asyncio.sleep(3)

        await self.check_and_wait_for_captcha()
        await self.check_and_close_signin()

    async def _harvest_page_results(self, obj_type):
        """Read the search responses the loaded search page fired itself.

        Keeps nudging the page with a scroll until we have both results and a
        captured param template for the endpoint: the request TikTok fires on page
        load doesn't always carry the full param block, and without a template the
        API route can't take over. Returns (results, has_more, cursor, search_id)
        so pagination continues from where the page's own requests left off. Each
        result is a (object, id) pair as produced by _yield_results.
        """
        results = []
        has_more = True
        cursor = 0
        search_id = ""
        seen_ids = set()
        attempts = 5

        for attempt in range(attempts):
            # Read the search_id off the page's own requests before consuming
            # their responses: that is the value the webapp itself echoes on
            # every page of this search, so replaying it is exact.
            search_id = search_id or self._seen_search_id(obj_type)

            for resp in await self.parent.process_pending_responses(_endpoint_path(obj_type)):
                res = self._parse_response(resp)
                if res is None:
                    continue
                page_size = 0
                for result, result_id in self._yield_results(obj_type, res, with_id=True):
                    page_size += 1
                    if result_id and result_id in seen_ids:
                        continue
                    if result_id:
                        seen_ids.add(result_id)
                    results.append((result, result_id))
                # Same rule as the other two routes: believe has_more=0 only
                # from a page short enough to be the last one.
                short_page = self._note_page_size(page_size)
                if not res.get("has_more", 1) and short_page:
                    has_more = False
                    self._exhausted_listing = True
                if isinstance(res.get("cursor"), int):
                    cursor = max(cursor, res["cursor"])
                # Fall back to the first response's logid, which is where the
                # webapp gets the search_id from in the first place. First wins:
                # later pages carry their own logid, which is not a search_id.
                search_id = search_id or (res.get("extra") or {}).get("logid", "")

            if results and self._has_api_template(obj_type):
                break
            if not has_more or attempt == attempts - 1:
                break
            await self.check_and_wait_for_captcha()
            await self.scroll_feed(container_selector=_SEARCH_GRID_SELECTOR)
            await asyncio.sleep(2.5)

        return results, has_more, cursor, search_id

    def _seen_search_id(self, obj_type):
        """The search_id the loaded page sent on its own pagination requests.

        The page's first request carries none (that response's logid becomes the
        search_id); every request after it repeats the same one.
        """
        for url in self.parent.seen_request_urls(_endpoint_path(obj_type)):
            search_id = parse_qs(urlparse(url).query).get("search_id", [""])[0]
            if search_id:
                return search_id
        return ""

    def _has_api_template(self, obj_type):
        """True once the webapp has been seen issuing a search request for this
        object type, i.e. the API route has params to replay."""
        return self.parent.tiktok_api.get_cached_api_params(
            f"https://www.tiktok.com/api/search/{obj_type}/full/"
        ) is not None

    def _parse_response(self, resp):
        """Decode a captured search response body, or None if unusable."""
        body = resp.get('body', '')
        if not body:
            return None
        try:
            res = json.loads(body) if isinstance(body, str) else body
        except json.JSONDecodeError:
            return None
        if res.get('type') == 'verify':
            # this is the captcha denied response
            return None
        return res

    async def _scroll_for_results(self, obj_type, count=28) -> Iterator:
        """Scroll the loaded search page, yielding results from each new response.

        `count` is how many results the caller still needs; it sizes the scroll
        budget, since the page hands back one page of results per scroll. None
        means the caller wants the whole listing, so stopping is left to has_more
        and the walk's stall check.
        """
        # One scroll yields one page, so a fixed budget silently caps big requests. Allow
        # enough scrolls for `count` with slack for rounds that come back empty, and let the
        # walk's stall check stop us early. count=None asks for everything, so there is no
        # budget to size -- then only the stall check ends the scroll.
        max_rounds = (_UNBOUNDED_MAX_ROUNDS if count is None
                      else max(30, math.ceil(count / _VIDEO_PAGE_SIZE) * 2))
        yielded = 0
        # The results live in an inner scroll container, not the window: scrolling the
        # window there is a no-op that never triggers the infinite-scroll observer.
        walk = self.scroll_walk(_endpoint_path(obj_type),
                                container_selector=_SEARCH_GRID_SELECTOR,
                                max_rounds=max_rounds,
                                before_round=self.check_and_wait_for_captcha)
        try:
            async for rnd in walk.rounds():
                for resp in rnd.responses:
                    res = self._parse_response(resp)
                    if res is None:
                        walk.stats['unusable_responses'] += 1
                        continue

                    results = list(self._yield_results(obj_type, res, with_id=True))
                    rnd.produced += len(results)
                    for result in results:
                        yielded += 1
                        yield result

                    # Only a short page is trusted when it says the listing is over (see
                    # _note_page_size). An empty or exactly-full page saying the same is as
                    # likely to be TikTok stalling us, so leave those to the walk's stall
                    # check, which gives the page a few more scrolls to recover.
                    short_page = self._note_page_size(len(results))
                    if not res.get("has_more", 0) and short_page:
                        self._exhausted_listing = True
                        rnd.stop = True
        finally:
            self.parent.logger.info(
                f"search {obj_type} '{self.search_term}': walked {yielded} results, "
                f"{walk.summary()}"
            )

    def _yield_results(self, obj_type, res, with_id=False):
        """Build User/Video objects from a search response payload."""
        if obj_type == "user":
            for result in res.get("user_list", []):
                info = result.get("user_info", {})
                obj = self.parent.user(
                    username=info.get("unique_id"),
                    user_id=info.get("user_id") or info.get("uid"),
                    sec_uid=info.get("sec_uid"),
                )
                result_id = info.get("user_id") or info.get("uid")
                yield (obj, result_id) if with_id else obj
        else:
            for result in res.get("item_list", []):
                obj = self.parent.video(data=result)
                result_id = result.get("id")
                yield (obj, result_id) if with_id else obj
