from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import TYPE_CHECKING, Iterator, Optional

from zendriver import cdp

from .user import User
from .video import Video
from .base import Base
from ..exceptions import *

if TYPE_CHECKING:
    from ..tiktok import PyTok

# web_search_code mirrors what the TikTok web app sends with search requests
_WEB_SEARCH_CODE = '{"tiktok":{"client_params_x":{"search_engine":{"ies_mt_user_live_video_card_use_libra":1,"mt_search_general_user_live_card":1}},"search_server":{}}}'

# TikTok's search results render inside an inner scroll container, not the
# document body — so window.scrollBy does nothing. Scroll that container to
# fire the lazily-loaded pagination requests. Fall back to the window in case
# the layout changes.
_SCROLL_SEARCH_GRID_JS = """
(() => {
  const el = document.querySelector('#grid-main') ||
             document.querySelector('[class*=SearchGridLayoutContainer]');
  if (el && el.scrollHeight > el.clientHeight) {
    el.scrollTop = el.scrollHeight;
    return;
  }
  window.scrollBy(0, window.innerHeight * 4);
})()
"""


class Search(Base):
    """Contains methods for searching TikTok."""

    parent: PyTok
    """The PyTok instance this object is bound to (set by api.search(...))."""

    def __init__(self, search_term, parent: Optional[PyTok] = None):
        self.parent = parent
        self.search_term = search_term

    async def videos(self, count=28, offset=0, **kwargs) -> Iterator[Video]:
        """
        Searches for Videos

        - Parameters:
            - count (int): The amount of videos you want returned.
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
            - count (int): The amount of users you want returned.
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
            - count (int): The amount of objects you want returned.
            - offset (int): The offset of objects you want returned.
            - obj_type (str): user | item
            - prefer_scraping (bool): If True, get results by scrolling the browser
              page rather than through the make_request API. Normally unnecessary:
              API requests replay the per-endpoint param template captured from the
              webapp's own search requests — lazily filled by the search page load
              below on the first search of a session (see
              ZendriverTikTokApi.cache_api_params) — changing only the keyword,
              cursor and search_id. Kept as an explicit escape hatch to force the
              scrolling path.
        """
        if obj_type not in ("user", "item"):
            raise TypeError("invalid obj_type")

        seen_ids = set()
        amount_yielded = 0

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
                if amount_yielded >= count:
                    return

        if not prefer_scraping:
            try:
                async for result in emit(self._search_type_api(obj_type, cursor=offset)):
                    yield result
                if amount_yielded > 0:
                    return
                self.parent.logger.warning(
                    f"API path returned no search results ({obj_type}). "
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
        if amount_yielded >= count:
            return
        if not has_more:
            return

        if not prefer_scraping:
            try:
                async for result in emit(
                    self._search_type_api(obj_type, cursor=cursor, search_id=search_id)
                ):
                    yield result
                return
            except ApiFailedException as ex:
                self.parent.logger.warning(
                    f"API still failing after the search page load ({ex}). "
                    "Continuing by scrolling the page."
                )

        async for result in emit(self._scroll_for_results(obj_type)):
            yield result

    @staticmethod
    async def _aiter(results):
        for result in results:
            yield result

    async def _search_type_api(self, obj_type, cursor=0, search_id="", **kwargs) -> Iterator:
        # TikTok ties all pages of one search to a search_id: the logid of the
        # first response. Without echoing it back on later pages the server
        # returns an empty item_list with has_more=0, capping results at 12. When
        # we resume after a page load we reuse the logid of the page's own
        # request, so the API continues that same search rather than starting a
        # fresh one.
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

            for result in self._yield_results(obj_type, res, with_id=True):
                yield result

            if not res.get("has_more", 0):
                self.parent.logger.info(
                    "TikTok is not sending results beyond this point."
                )
                return

            cursor = res.get("cursor", cursor)
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
        page = self.parent._page
        results = []
        has_more = True
        cursor = 0
        search_id = ""
        seen_ids = set()
        attempts = 5

        for attempt in range(attempts):
            for resp in await self.parent.process_pending_responses(f"api/search/{obj_type}/"):
                res = self._parse_response(resp)
                if res is None:
                    continue
                for result, result_id in self._yield_results(obj_type, res, with_id=True):
                    if result_id and result_id in seen_ids:
                        continue
                    if result_id:
                        seen_ids.add(result_id)
                    results.append((result, result_id))
                has_more = res.get("has_more", 0)
                cursor = res.get("cursor", cursor)
                search_id = (res.get("extra") or {}).get("logid", "") or search_id

            if results and self._has_api_template(obj_type):
                break
            if not has_more or attempt == attempts - 1:
                break
            await self.check_and_wait_for_captcha()
            await page.evaluate(_SCROLL_SEARCH_GRID_JS)
            await asyncio.sleep(2.5)

        return results, has_more, cursor, search_id

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

    async def _scroll_for_results(self, obj_type) -> Iterator:
        """Scroll the loaded search page, yielding results from each new response."""
        page = self.parent._page

        has_more = True
        scroll_attempts = 0
        max_scroll_attempts = 30
        empty_rounds = 0
        max_empty_rounds = 3

        while has_more and scroll_attempts < max_scroll_attempts:
            await self.check_and_wait_for_captcha()

            # Scroll first so the lazily-loaded search request fires, then give
            # its response body time to be captured before reading it. The
            # results live in an inner scroll container (#grid-main), not the
            # window — scrolling the window is a no-op and never triggers the
            # infinite-scroll observer, so target the container.
            yielded_this_round = 0
            await page.evaluate(_SCROLL_SEARCH_GRID_JS)
            await asyncio.sleep(3)
            await self.check_and_resolve_refresh_button()

            responses = await self.parent.process_pending_responses("api/search/")
            for resp in responses:
                res = self._parse_response(resp)
                if res is None:
                    continue

                for result in self._yield_results(obj_type, res, with_id=True):
                    yielded_this_round += 1
                    yield result

                if not res.get("has_more", 0):
                    self.parent.logger.info(
                        "TikTok is not sending results beyond this point."
                    )
                    has_more = False

            if not has_more:
                break

            # Give up early if scrolling stops producing new results rather than
            # scrolling all the way to the hard limit.
            if yielded_this_round == 0:
                empty_rounds += 1
                if empty_rounds >= max_empty_rounds:
                    self.parent.logger.info(
                        "No new search results after repeated scrolls, stopping."
                    )
                    return
            else:
                empty_rounds = 0

            await self.parent.request_delay()
            scroll_attempts += 1

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
