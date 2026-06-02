from __future__ import annotations

import asyncio
import json

from typing import TYPE_CHECKING, ClassVar, Iterator, Optional

from zendriver import cdp

if TYPE_CHECKING:
    from ..tiktok import PyTok
    from .video import Video

from .base import Base
from ..exceptions import *


class Hashtag(Base):
    """
    A TikTok Hashtag/Challenge.

    Example Usage
    ```py
    hashtag = api.hashtag(name='funny')
    ```
    """

    parent: ClassVar[PyTok]

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
    ):
        """
        You must provide the name or id of the hashtag.
        """
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
        page = self.parent._page

        url = f"https://www.tiktok.com/tag/{self.name}"
        self.parent.logger.debug(f"Loading page: {url}")
        await page.send(cdp.page.navigate(url))
        async with asyncio.timeout(30):
            await page.wait_for_ready_state(until='complete', timeout=31)
        await asyncio.sleep(3)  # Brief wait for dynamic content

        await self.parent.process_pending_responses()
        await self.wait_for_content_or_unavailable_or_captcha('[data-e2e=challenge-item]', 'Not available')
        await self.check_and_close_signin()

        challenge_responses = await self.parent.process_pending_responses("api/challenge/detail")
        if len(challenge_responses) == 0:
            raise ApiFailedException("Failed to get challenge response")

        rep_body = challenge_responses[0].get('body', '')
        rep_d = json.loads(rep_body) if isinstance(rep_body, str) else rep_body

        if 'challengeInfo' not in rep_d:
            raise ApiFailedException("Failed to get challengeInfo from response")

        self.as_dict = rep_d['challengeInfo']
        self.__extract_from_data()
        return self.as_dict

    async def videos(self, count=30, offset=0, **kwargs) -> Iterator[Video]:
        """Returns a dictionary listing TikToks with a specific hashtag.

        - Parameters:
            - count (int): The amount of videos you want returned.
            - offset (int): The the offset of videos from 0 you want to get.

        Example Usage
        ```py
        async for video in api.hashtag(name='funny').videos():
            # do something
        ```
        """
        await self.info()

        try:
            async for video in self._get_videos_api(count, offset, **kwargs):
                yield video
        except ApiFailedException as ex:
            self.parent.logger.warning(
                f"TikTok-Api hashtag.videos() failed: {ex}. Falling back to scraping method."
            )
            async for video in self._get_videos_scraping(count, offset, **kwargs):
                yield video

    async def _get_videos_api(self, count=30, offset=0, **kwargs):
        amount_yielded = 0
        cursor = offset

        while amount_yielded < count:
            params = {
                "challengeID": self.id,
                "count": 35,
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

            # challenge/item_list needs anti-bot params (verifyFp etc.) that
            # make_request doesn't add, so it often returns a non-zero status
            # with no items. Treat that as a failure so we fall back to scraping.
            status_code = res.get('statusCode', 0)
            if status_code != 0:
                raise ApiFailedException(
                    f"TikTok returned error for hashtag videos: statusCode={status_code}"
                )

            videos = res.get("itemList", [])
            for video in videos:
                yield self.parent.video(data=video)
                amount_yielded += 1
                if amount_yielded >= count:
                    return

            if not res.get("hasMore", False):
                self.parent.logger.info(
                    "TikTok isn't sending more TikToks beyond this point."
                )
                return

            cursor = res.get("cursor", cursor)
            await self.parent.request_delay()

    async def _get_videos_scraping(self, count=30, offset=0, **kwargs):
        page = self.parent._page

        # Ensure we're on the hashtag page so item_list requests fire as we scroll
        url = f"https://www.tiktok.com/tag/{self.name}"
        self.parent.logger.debug(f"Loading page: {url}")
        await page.send(cdp.page.navigate(url))
        async with asyncio.timeout(30):
            await page.wait_for_ready_state(until='complete', timeout=31)
        await asyncio.sleep(3)

        await self.parent.process_pending_responses()
        await self.check_and_wait_for_captcha()
        await self.check_and_close_signin()
        if not await self._is_selector_visible('[data-e2e=challenge-item]'):
            self.parent.logger.warning(
                "Hashtag video grid not visible yet (TikTok requires login for this "
                "feed; pass a logged-in user_data_dir if you get no results)."
            )

        amount_yielded = 0
        seen_ids = set()
        has_more = True
        scroll_attempts = 0
        max_scroll_attempts = 30
        empty_rounds = 0
        max_empty_rounds = 5

        while amount_yielded < count and has_more and scroll_attempts < max_scroll_attempts:
            await self.check_and_wait_for_captcha()

            # Scroll first so the lazily-loaded item_list request fires, then give
            # its response body time to be captured before reading it.
            yielded_before = amount_yielded
            await page.evaluate('window.scrollBy(0, window.innerHeight * 4)')
            await asyncio.sleep(3)
            await self.check_and_resolve_refresh_button()

            video_responses = await self.parent.process_pending_responses("api/challenge/item_list")
            for resp in video_responses:
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

                for video in res.get("itemList", []):
                    video_id = video.get('id')
                    if video_id and video_id in seen_ids:
                        continue
                    if video_id:
                        seen_ids.add(video_id)
                    yield self.parent.video(data=video)
                    amount_yielded += 1
                    if amount_yielded >= count:
                        return

                if not res.get("hasMore", False):
                    self.parent.logger.info(
                        "TikTok isn't sending more TikToks beyond this point."
                    )
                    has_more = False

            if not has_more:
                break

            # Give up early if scrolling stops producing new videos (e.g. the
            # feed is login-walled) rather than scrolling to the hard limit.
            if amount_yielded == yielded_before:
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
            Hashtag.parent.logger.error(
                f"Failed to create Hashtag with data: {data}\nwhich has keys {data.keys()}"
            )

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return f"PyTok.hashtag(id='{self.id}', name='{self.name}')"
