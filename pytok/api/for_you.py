from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Iterator, Optional

from ..exceptions import ApiFailedException, CaptchaException
from .base import Base

if TYPE_CHECKING:
    from ..tiktok import PyTok
    from .video import Video

FOR_YOU_URL = "https://www.tiktok.com/foryou"
RECOMMEND_URL = "https://www.tiktok.com/api/recommend/item_list/"
RECOMMEND_PATTERN = "api/recommend/item_list"

# The feed is a one-video-per-screen swiper: ArrowDown moves to the next video, and
# the page asks for the next batch only as its buffer runs low, a few videos ahead.
PRESSES_PER_ROUND = 12
# The feed never says it has ended, so a walk stops once this many rounds in a row
# bring nothing new.
STALL_ROUNDS = 3
# Seconds to wait for the page's first batch after navigating.
FIRST_PAGE_TIMEOUT = 30


class ForYou(Base):
    """The logged-in account's For You (home) feed.

    What it returns is personalised to the account and changes with every read, so a
    walk is a sample of what TikTok recommends right now, not a listing to be exhausted.

    Example Usage
    ```py
    async for video in api.for_you().videos(count=50):
        ...
    ```
    """

    parent: PyTok

    def __init__(self, parent: Optional[PyTok] = None):
        self.parent = parent

    async def videos(self, count=30, prefer_scraping=False, get_media=False, **kwargs) -> Iterator[Video]:
        """Yield Videos from the For You feed.

        - Parameters:
            - count (int): How many distinct videos to return.
            - prefer_scraping (bool): Advance the feed only by moving through it in the
              page, the way a viewer would, instead of replaying the page's own
              recommend request. Slower, but every video is actually shown to the
              account, which matters if what TikTok recommends next is the point.
            - get_media (bool | callable): Download each post's media into
              ``video.post_media`` as it is yielded. See User.videos().
        """
        videos = self._iter_videos(count=count, prefer_scraping=prefer_scraping)
        async for video in self._with_media(videos, get_media):
            yield video

    async def _iter_videos(self, count, prefer_scraping):
        seen = set()
        yielded = 0
        stalled = 0
        api_usable = not prefer_scraping

        batch = await self._load_feed()
        while True:
            produced = 0
            for item in batch:
                # a batch can list the same video twice, so check each against what has
                # already gone out, this batch included
                if not item.get('id') or item['id'] in seen:
                    continue
                seen.add(item['id'])
                produced += 1
                yield self.parent.video(data=item)
                yielded += 1
                if yielded >= count:
                    return

            stalled = 0 if produced else stalled + 1
            if stalled >= STALL_ROUNDS:
                self.parent.logger.info(
                    f"For You feed stopped producing new videos after {yielded}; "
                    f"{STALL_ROUNDS} rounds in a row brought nothing new")
                return

            batch = None
            if api_usable:
                try:
                    batch = await self._api_batch()
                except ApiFailedException as ex:
                    # A refused replay is usually transient, so move the page on
                    # instead, which also refreshes the template, then try again.
                    self.parent.logger.debug(f"For You API batch failed ({ex}); advancing the page instead")
            if batch is None:
                batch = await self._advance_feed()

    async def _load_feed(self):
        """Open the For You page and return the videos of its first batch."""
        # drop anything an earlier page left queued under this pattern
        await self.parent.process_pending_responses(RECOMMEND_PATTERN)
        await self.parent.navigate(FOR_YOU_URL)
        await self.check_and_wait_for_captcha()
        await self.check_and_close_signin()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + FIRST_PAGE_TIMEOUT
        while loop.time() < deadline:
            items = self._items(await self.parent.process_pending_responses(RECOMMEND_PATTERN))
            if items:
                return items
            await asyncio.sleep(0.5)
        raise ApiFailedException(
            f"The For You page sent no recommendations within {FIRST_PAGE_TIMEOUT}s; "
            "the feed needs a logged-in session")

    async def _api_batch(self):
        """One batch from replaying the page's recommend request."""
        try:
            res = await self.parent.tiktok_api.make_request(url=RECOMMEND_URL, params={})
        except ApiFailedException:
            raise
        except Exception as ex:
            raise ApiFailedException(f"recommend request failed: {ex}")
        if not isinstance(res, dict) or res.get('type') == 'verify':
            raise ApiFailedException("recommend request was refused")
        if res.get('statusCode') not in (0, None):
            raise ApiFailedException(f"recommend request returned statusCode={res.get('statusCode')}")
        return res.get('itemList') or []

    async def _advance_feed(self):
        """Move through the feed until the page fetches its next batch.

        Returns that batch's videos, or [] if the page asked for nothing.
        """
        page = self.parent._page
        for _ in range(PRESSES_PER_ROUND):
            await page.keyboard.press("ArrowDown")
            # returns quickly when the press fired no request, which is most of them
            responses, _ = await self.await_api_responses(RECOMMEND_PATTERN)
            items = self._items(responses)
            if items:
                return items
            await asyncio.sleep(self.scroll_round_floor())
        if await self._is_captcha_visible():
            raise CaptchaException("Captcha shown while moving through the For You feed")
        return []

    @staticmethod
    def _items(responses):
        items = []
        for resp in responses:
            body = resp.get('body')
            try:
                res = json.loads(body) if isinstance(body, str) else body
            except json.JSONDecodeError:
                continue
            if isinstance(res, dict):
                items.extend(res.get('itemList') or [])
        return items
