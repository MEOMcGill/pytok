from __future__ import annotations

import asyncio
import json

from typing import TYPE_CHECKING, Iterator, Optional

from zendriver import cdp

if TYPE_CHECKING:
    from ..tiktok import PyTok
    from .video import Video

from .base import Base
from ..exceptions import *
from ..helpers import extract_tag_contents


class Hashtag(Base):
    """
    A TikTok Hashtag/Challenge.

    Example Usage
    ```py
    hashtag = api.hashtag(name='funny')
    ```
    """

    parent: PyTok
    """The PyTok instance this object is bound to (set by api.hashtag(...))."""

    id: Optional[str]
    """The ID of the hashtag"""
    name: Optional[str]
    """The name of the hashtag (omiting the #)"""
    as_dict: dict
    """The raw data associated with this hashtag."""

    def __init__(
        self,
        name: Optional[str] = None,
        id: Optional[str] = None,
        data: Optional[dict] = None,
        parent: Optional[PyTok] = None,
    ):
        """
        You must provide the name or id of the hashtag.
        """
        self.parent = parent
        self.name = name
        self.id = id

        if data is not None:
            self.as_dict = data
            self.__extract_from_data()
        else:
            self.as_dict = None

    async def info(self, **kwargs) -> dict:
        """
        Returns TikTok's dictionary representation of the hashtag object.
        """
        if self.as_dict is None:
            return await self.info_full(**kwargs)
        return self.as_dict

    async def info_full(self, **kwargs) -> dict:
        """
        Returns all information sent by TikTok related to this hashtag.

        Example Usage
        ```py
        hashtag_data = await api.hashtag(name='funny').info_full()
        ```
        """
        try:
            return await self._info_full_api(**kwargs)
        except ApiFailedException as ex:
            self.parent.logger.warning(
                f"TikTok-Api hashtag.info_full() failed: {ex}. Falling back to scraping method."
            )
            return await self._info_full_scrape(**kwargs)

    async def _info_full_api(self, **kwargs) -> dict:
        if not self.name:
            raise TypeError(
                "You must provide the name when creating this class to use this method."
            )

        url_params = {
            "challengeName": self.name,
        }

        try:
            resp = await self.parent.tiktok_api.make_request(
                url="https://www.tiktok.com/api/challenge/detail/",
                params=url_params,
            )
        except EmptyResponseException:
            raise ApiFailedException("TikTok API returned empty response")
        except Exception as e:
            raise ApiFailedException(f"TikTok-Api make_request failed: {e}")

        if resp is None:
            raise ApiFailedException("TikTok returned None response")

        if 'challengeInfo' not in resp:
            raise ApiFailedException("Failed to get challengeInfo from response")

        self.as_dict = resp['challengeInfo']
        self.__extract_from_data()
        return self.as_dict

    async def _info_full_scrape(self, **kwargs) -> dict:
        await self._navigate_to_hashtag_page()
        await self.wait_for_content_or_unavailable_or_captcha('[data-e2e=challenge-item]', 'Not available')
        await self.check_and_close_signin()

        # The challenge/detail response can land after the grid renders, so poll
        # for it rather than reading once.
        challenge_info = None
        for _ in range(5):
            for resp in await self.parent.process_pending_responses("api/challenge/detail"):
                res = self._parse_response(resp)
                if res and res.get('challengeInfo'):
                    challenge_info = res['challengeInfo']
            if challenge_info is not None:
                break
            await asyncio.sleep(1.5)

        if challenge_info is None:
            # Chrome can garbage-collect a response body before we read it; the
            # page's own rehydration JSON carries the same object.
            challenge_info = self._challenge_info_from_html(
                await self.parent._page.get_content()
            )

        if challenge_info is None:
            raise ApiFailedException("Failed to get challengeInfo from the hashtag page")

        self.as_dict = challenge_info
        self.__extract_from_data()
        return self.as_dict

    def _challenge_info_from_html(self, html):
        """Pull challengeInfo out of the hashtag page's rehydration JSON."""
        tag_contents = extract_tag_contents(html)
        if not tag_contents:
            return None
        try:
            data = json.loads(tag_contents)
        except json.JSONDecodeError:
            return None
        detail = data.get('__DEFAULT_SCOPE__', {}).get('webapp.challenge-detail', {})
        return detail.get('challengeInfo') or None

    async def videos(self, count=30, offset=0, prefer_scraping=False, **kwargs) -> Iterator[Video]:
        """Returns a dictionary listing TikToks with a specific hashtag.

        - Parameters:
            - count (int): The amount of videos you want returned.
            - offset (int): The the offset of videos from 0 you want to get.
            - prefer_scraping (bool): If True, get videos by scrolling the browser page
              rather than through the make_request API. Normally unnecessary: API
              requests replay the per-endpoint param template captured from the
              webapp's own challenge/item_list requests — lazily filled by the page
              load below on the first hashtag of a session (see
              ZendriverTikTokApi.cache_api_params) — changing only challengeID and
              cursor. Kept as an explicit escape hatch to force the scrolling path.

        Example Usage
        ```py
        async for video in api.hashtag(name='funny').videos():
            # do something
        ```
        """
        await self.info()

        seen_ids = set()
        amount_yielded = 0

        async def emit(source):
            """Yield videos from a source, deduping and honouring `count`."""
            nonlocal amount_yielded
            async for video in source:
                video_id = getattr(video, 'id', None)
                if video_id is not None:
                    if video_id in seen_ids:
                        continue
                    seen_ids.add(video_id)
                amount_yielded += 1
                yield video
                if amount_yielded >= count:
                    return

        if not prefer_scraping:
            try:
                async for video in emit(self._get_videos_api(cursor=offset)):
                    yield video
                if amount_yielded > 0:
                    return
                self.parent.logger.warning(
                    "API path returned no hashtag videos. Falling back to scraping method."
                )
            except ApiFailedException as ex:
                self.parent.logger.warning(
                    f"TikTok-Api hashtag.videos() failed: {ex}. Falling back to scraping method."
                )

        # Scraping route. Loading the hashtag page fires the webapp's own
        # challenge/item_list request, which fills the param template for that
        # endpoint (PyTok._on_request_will_be_sent -> cache_api_params). So we
        # harvest that first page off the wire and then resume paginating through
        # the API from its cursor, instead of scrolling for every page.
        await self._load_hashtag_page()
        items, has_more, cursor = await self._harvest_page_videos()
        self.parent.logger.info(
            f"Got {len(items)} videos from the hashtag page, hasMore={has_more}, cursor={cursor}"
        )
        async for video in emit(self._iter_video_objs(items)):
            yield video
        if amount_yielded >= count:
            return
        if not has_more:
            return

        if not prefer_scraping:
            try:
                async for video in emit(self._get_videos_api(cursor=cursor)):
                    yield video
                return
            except ApiFailedException as ex:
                self.parent.logger.warning(
                    f"API still failing after the hashtag page load ({ex}). "
                    "Continuing by scrolling the page."
                )

        async for video in emit(self._scroll_for_videos()):
            yield video

    async def _iter_video_objs(self, items):
        for item in items:
            yield self.parent.video(data=item)

    async def _get_videos_api(self, cursor=0, **kwargs):
        # `count` is deliberately not sent: the cached param template carries the
        # page size the webapp itself used for this endpoint, and matching the
        # frontend's request shape is the whole point of replaying the template.
        while True:
            params = {
                "challengeID": self.id,
                "cursor": cursor,
            }

            try:
                res = await self.parent.tiktok_api.make_request(
                    url="https://www.tiktok.com/api/challenge/item_list/",
                    params=params,
                )
            except Exception as e:
                raise ApiFailedException(f"TikTok-Api make_request failed: {e}")

            if res is None:
                raise ApiFailedException("TikTok-Api returned None response")

            if res.get('type') == 'verify':
                raise ApiFailedException("TikTok API is asking for verification")

            # A non-zero status here means the replayed params were rejected (no
            # template yet, or a stale/burned one — make_request drops the
            # template in that case). Fail so the caller falls back to the page
            # load, which re-captures a fresh template off the wire.
            status_code = res.get('statusCode', 0)
            if status_code != 0:
                raise ApiFailedException(
                    f"TikTok returned error for hashtag videos: statusCode={status_code}"
                )

            for video in res.get("itemList", []):
                yield self.parent.video(data=video)

            if not res.get("hasMore", False):
                self.parent.logger.info(
                    "TikTok isn't sending more TikToks beyond this point."
                )
                return

            cursor = res.get("cursor", cursor)
            await self.parent.request_delay()

    async def _navigate_to_hashtag_page(self):
        """Load the hashtag page.

        The page load fires the webapp's own challenge/detail and
        challenge/item_list requests, which fill the param templates for those
        endpoints (PyTok._on_request_will_be_sent -> cache_api_params).
        """
        page = self.parent._page

        # Drop anything captured for earlier operations first, so what we harvest
        # after the navigation belongs to this hashtag.
        await self.parent.process_pending_responses()

        url = f"https://www.tiktok.com/tag/{self.name}"
        self.parent.logger.debug(f"Loading page: {url}")
        await page.send(cdp.page.navigate(url))
        async with asyncio.timeout(30):
            await page.wait_for_ready_state(until='complete', timeout=31)
        await asyncio.sleep(3)

    async def _load_hashtag_page(self):
        """Make sure the hashtag page is loaded, ready to be harvested."""
        url = f"https://www.tiktok.com/tag/{self.name}"
        if (self.parent._page.url or '').startswith(url):
            # info() already scraped this page and its item_list responses are
            # still queued — reloading would throw them away.
            self.parent.logger.debug("Already on the hashtag page, not reloading")
            return

        await self._navigate_to_hashtag_page()
        await self.check_and_wait_for_captcha()
        await self.check_and_close_signin()
        if not await self._is_selector_visible('[data-e2e=challenge-item]'):
            self.parent.logger.warning(
                "Hashtag video grid not visible yet (TikTok requires login for this "
                "feed; pass a logged-in user_data_dir if you get no results)."
            )

    async def _harvest_page_videos(self):
        """Read the item_list responses the loaded hashtag page fired itself.

        Keeps nudging the page with a scroll until we have both videos and a
        captured param template for the endpoint: the request TikTok fires on page
        load doesn't always carry the full param block, and without a template the
        API route can't take over. Returns (items, has_more, cursor) so pagination
        continues from where the page's own requests left off.
        """
        page = self.parent._page
        items = []
        has_more = True
        cursor = 0
        seen_ids = set()
        attempts = 5

        for attempt in range(attempts):
            responses = await self.parent.process_pending_responses("api/challenge/item_list")
            # Prefer responses for this challenge, but don't discard everything if
            # TikTok changes how the request identifies the hashtag.
            for_this_hashtag = [
                resp for resp in responses
                if f"challengeID={self.id}" in resp.get('url', '')
            ]
            for resp in for_this_hashtag or responses:
                res = self._parse_response(resp)
                if res is None:
                    continue
                for video in res.get("itemList", []):
                    video_id = video.get('id')
                    if video_id and video_id in seen_ids:
                        continue
                    if video_id:
                        seen_ids.add(video_id)
                    items.append(video)
                has_more = res.get("hasMore", False)
                cursor = res.get("cursor", cursor)

            if items and self._has_api_template():
                break
            if not has_more or attempt == attempts - 1:
                break
            await self.check_and_wait_for_captcha()
            await page.evaluate('window.scrollBy(0, window.innerHeight * 4)')
            await asyncio.sleep(2.5)

        return items, has_more, cursor

    def _has_api_template(self):
        """True once the webapp has been seen issuing a challenge/item_list
        request, i.e. the API route has params to replay."""
        return self.parent.tiktok_api.get_cached_api_params(
            "https://www.tiktok.com/api/challenge/item_list/"
        ) is not None

    def _parse_response(self, resp):
        """Decode a captured item_list response body, or None if unusable."""
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

    async def _scroll_for_videos(self):
        """Scroll the loaded hashtag page, yielding videos from each new response."""
        page = self.parent._page

        has_more = True
        scroll_attempts = 0
        max_scroll_attempts = 30
        empty_rounds = 0
        max_empty_rounds = 5

        while has_more and scroll_attempts < max_scroll_attempts:
            await self.check_and_wait_for_captcha()

            # Scroll first so the lazily-loaded item_list request fires, then give
            # its response body time to be captured before reading it.
            yielded_this_round = 0
            await page.evaluate('window.scrollBy(0, window.innerHeight * 4)')
            await asyncio.sleep(3)
            await self.check_and_resolve_refresh_button()

            video_responses = await self.parent.process_pending_responses("api/challenge/item_list")
            for resp in video_responses:
                res = self._parse_response(resp)
                if res is None:
                    continue

                for video in res.get("itemList", []):
                    yielded_this_round += 1
                    yield self.parent.video(data=video)

                if not res.get("hasMore", False):
                    self.parent.logger.info(
                        "TikTok isn't sending more TikToks beyond this point."
                    )
                    has_more = False

            if not has_more:
                break

            # Give up early if scrolling stops producing new videos (e.g. the
            # feed is login-walled) rather than scrolling to the hard limit.
            if yielded_this_round == 0:
                empty_rounds += 1
                if empty_rounds >= max_empty_rounds:
                    self.parent.logger.info(
                        "No new hashtag videos after repeated scrolls, stopping."
                    )
                    return
            else:
                empty_rounds = 0

            await self.parent.request_delay()
            scroll_attempts += 1

    def __extract_from_data(self):
        data = self.as_dict
        keys = data.keys()

        if "title" in keys:
            self.id = data["id"]
            self.name = data["title"]

        if "challenge" in keys:
            self.id = data["challenge"]["id"]
            self.name = data["challenge"]["title"]

        if None in (self.name, self.id):
            self.parent.logger.error(
                f"Failed to create Hashtag with data: {data}\nwhich has keys {data.keys()}"
            )

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return f"PyTok.hashtag(id='{self.id}', name='{self.name}')"
