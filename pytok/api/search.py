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

        Example Usage
        ```py
        async for user in api.search('therock').users():
            # do something
        ```
        """
        async for result in self.search_type("user", count=count, offset=offset, **kwargs):
            yield result

    async def search_type(self, obj_type, count=28, offset=0, **kwargs) -> Iterator:
        """
        Searches for a specific type of object. Use .videos() & .users() instead.

        - Parameters:
            - count (int): The amount of objects you want returned.
            - offset (int): The offset of objects you want returned.
            - obj_type (str): user | item
        """
        if obj_type not in ("user", "item"):
            raise TypeError("invalid obj_type")

        try:
            async for result in self._search_type_api(obj_type, count=count, offset=offset, **kwargs):
                yield result
        except ApiFailedException as ex:
            self.parent.logger.warning(
                f"TikTok-Api search ({obj_type}) failed: {ex}. Falling back to scraping method."
            )
            async for result in self._search_type_scraping(obj_type, count=count, offset=offset, **kwargs):
                yield result

    async def _search_type_api(self, obj_type, count=28, offset=0, **kwargs) -> Iterator:
        amount_yielded = 0
        cursor = offset
        # TikTok ties all pages of one search to a search_id: the logid of the
        # first response. Without echoing it back on later pages the server
        # returns an empty item_list with has_more=0, capping results at 12.
        search_id = ""

        while amount_yielded < count:
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

            for result in self._yield_results(obj_type, res):
                yield result
                amount_yielded += 1
                if amount_yielded >= count:
                    return

            if not res.get("has_more", 0):
                self.parent.logger.info(
                    "TikTok is not sending results beyond this point."
                )
                return

            cursor = res.get("cursor", cursor)
            await self.parent.request_delay()

    async def _search_type_scraping(self, obj_type, count=28, offset=0, **kwargs) -> Iterator:
        page = self.parent._page

        subpath = "user" if obj_type == "user" else "video"
        url = f"https://www.tiktok.com/search/{subpath}?q={urllib.parse.quote(self.search_term)}"
        self.parent.logger.debug(f"Loading page: {url}")
        await page.send(cdp.page.navigate(url))
        async with asyncio.timeout(30):
            await page.wait_for_ready_state(until='complete', timeout=31)
        await asyncio.sleep(3)

        await self.parent.process_pending_responses()
        await self.check_and_wait_for_captcha()
        await self.check_and_close_signin()

        amount_yielded = 0
        seen_ids = set()
        has_more = True
        scroll_attempts = 0
        max_scroll_attempts = 30
        empty_rounds = 0
        max_empty_rounds = 3

        while amount_yielded < count and has_more and scroll_attempts < max_scroll_attempts:
            await self.check_and_wait_for_captcha()

            # Scroll first so the lazily-loaded search request fires, then give
            # its response body time to be captured before reading it. The
            # results live in an inner scroll container (#grid-main), not the
            # window — scrolling the window is a no-op and never triggers the
            # infinite-scroll observer, so target the container.
            yielded_before = amount_yielded
            await page.evaluate(_SCROLL_SEARCH_GRID_JS)
            await asyncio.sleep(3)
            await self.check_and_resolve_refresh_button()

            responses = await self.parent.process_pending_responses("api/search/")
            for resp in responses:
                body = resp.get('body', '')
                if not body:
                    continue
                try:
                    res = json.loads(body) if isinstance(body, str) else body
                except json.JSONDecodeError:
                    continue
                if res.get('type') == 'verify':
                    # this is the captcha denied response
                    continue

                for result, result_id in self._yield_results(obj_type, res, with_id=True):
                    if result_id and result_id in seen_ids:
                        continue
                    if result_id:
                        seen_ids.add(result_id)
                    yield result
                    amount_yielded += 1
                    if amount_yielded >= count:
                        return

                if not res.get("has_more", 0):
                    self.parent.logger.info(
                        "TikTok is not sending results beyond this point."
                    )
                    has_more = False

            if not has_more:
                break

            # Give up early if scrolling stops producing new results rather than
            # scrolling all the way to the hard limit.
            if amount_yielded == yielded_before:
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
