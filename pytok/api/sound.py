from __future__ import annotations

import asyncio
import json
import re

from typing import TYPE_CHECKING, Iterator, Optional
from urllib.parse import urlparse

from zendriver import cdp

if TYPE_CHECKING:
    from ..tiktok import PyTok
    from .user import User
    from .video import Video

from .base import Base
from ..exceptions import *
from ..helpers import extract_tag_contents


# The sound feed's items, used both to wait for the feed and to report how far it has
# grown during a scroll walk.
MUSIC_ITEM_SELECTOR = '[data-e2e=music-item]'


class Sound(Base):
    """
    A TikTok Sound/Music/Song.

    Example Usage
    ```py
    sound = api.sound(id='7016547803243022337')
    ```
    """

    parent: PyTok
    """The PyTok instance this object is bound to (set by api.sound(...))."""

    id: str
    """TikTok's ID for the sound"""
    title: Optional[str]
    """The title of the song."""
    author: Optional[User]
    """The author of the song (if it exists)"""
    as_dict: Optional[dict]
    """The raw data associated with this sound."""

    def __init__(
        self,
        id: Optional[str] = None,
        data: Optional[dict] = None,
        parent: Optional[PyTok] = None,
    ):
        """
        You must provide the id of the sound or it will not work.
        """
        self.parent = parent
        self.id = str(id) if id is not None else None
        self.title = None
        self.author = None

        if data is not None:
            self.as_dict = data
            self.__extract_from_data()
        elif self.id is None:
            raise TypeError("You must provide id parameter.")
        else:
            self.as_dict = None

    async def info(self, **kwargs) -> dict:
        """
        Returns TikTok's dictionary representation of the sound object.

        Example Usage
        ```py
        sound_data = await api.sound(id='7016547803243022337').info()
        ```
        """
        if self.as_dict is None:
            return await self.info_full(**kwargs)
        return self.as_dict

    async def info_full(self, **kwargs) -> dict:
        """
        Returns all information sent by TikTok related to this sound.

        Example Usage
        ```py
        sound_data = await api.sound(id='7016547803243022337').info_full()
        ```
        """
        try:
            return await self._info_full_api(**kwargs)
        except ApiFailedException as ex:
            self.parent.logger.warning(
                f"API sound info request failed: {ex}. Falling back to scraping method."
            )
            return await self._info_full_scrape(**kwargs)

    async def _info_full_api(self, **kwargs) -> dict:
        self.__ensure_valid()

        url_params = {
            "musicId": self.id,
        }

        try:
            resp = await self.parent.tiktok_api.make_request(
                url="https://www.tiktok.com/api/music/detail/",
                params=url_params,
            )
        except EmptyResponseException:
            raise ApiFailedException("TikTok API returned empty response")
        except Exception as e:
            raise ApiFailedException(f"API request failed: {e}")

        if resp is None:
            raise ApiFailedException("TikTok returned None response")

        if 'musicInfo' not in resp:
            raise ApiFailedException("Failed to get musicInfo from response")

        self.as_dict = resp['musicInfo']
        self.__extract_from_data()
        return self.as_dict

    async def _info_full_scrape(self, **kwargs) -> dict:
        await self._navigate_to_sound_page()

        # Read the detail response the page fired on load straight away: Chrome
        # garbage-collects response bodies, and the DOM waits below can easily
        # outlast this one.
        music_info = await self._read_music_detail_responses()

        if music_info is None:
            await self.wait_for_content_or_unavailable_or_captcha(
                MUSIC_ITEM_SELECTOR, 'Not available'
            )
            await self.check_and_close_signin()

            # The music/detail response can also land after the grid renders, so
            # poll for it rather than reading once.
            for _ in range(5):
                music_info = await self._read_music_detail_responses()
                if music_info is not None:
                    break
                await asyncio.sleep(1.5)

        if music_info is None:
            # Chrome can garbage-collect a response body before we read it. The
            # music page doesn't normally ship a music-detail rehydration scope
            # the way the hashtag page does, but check anyway in case TikTok
            # server-renders it for this visitor.
            music_info = self._music_info_from_html(
                await self.parent._page.get_content()
            )

        if music_info is None:
            # The page load has now filled the param template for music/detail
            # (PyTok._on_request_will_be_sent -> cache_api_params), so the API
            # route can have another go — the first attempt may simply have had
            # no template to replay.
            try:
                return await self._info_full_api(**kwargs)
            except ApiFailedException as ex:
                self.parent.logger.warning(
                    f"music/detail still failing after the sound page load ({ex}). "
                    "Falling back to the sound metadata carried by the video feed."
                )

        if music_info is None:
            music_info = await self._music_info_from_item_list()

        if music_info is None:
            raise ApiFailedException("Failed to get musicInfo from the sound page")

        self.as_dict = music_info
        self.__extract_from_data()
        return self.as_dict

    async def _read_music_detail_responses(self):
        """Return the musicInfo of any captured music/detail response, else None."""
        music_info = None
        for resp in await self.parent.process_pending_responses("api/music/detail"):
            res = self._parse_response(resp)
            if res and res.get('musicInfo'):
                music_info = res['musicInfo']
        return music_info

    async def _music_info_from_item_list(self):
        """Last-resort metadata: every video in the feed carries the sound it was
        made with, so read our own sound off the page's item_list responses.

        Thinner than music/detail — no play/video counts — but enough for the id,
        title and author. The responses are put back on the queue afterwards so a
        following videos() call can still harvest them.
        """
        responses = await self.parent.process_pending_responses("api/music/item_list")
        self.parent._collected_responses.extend(responses)

        for resp in responses:
            res = self._parse_response(resp)
            if res is None:
                continue
            for item in res.get("itemList", []):
                music = item.get("music") or {}
                if str(music.get("id")) == self.id:
                    return {"music": music}
        return None

    def _music_info_from_html(self, html):
        """Pull musicInfo out of the sound page's rehydration JSON, if present."""
        tag_contents = extract_tag_contents(html)
        if not tag_contents:
            return None
        try:
            data = json.loads(tag_contents)
        except json.JSONDecodeError:
            return None
        detail = data.get('__DEFAULT_SCOPE__', {}).get('webapp.music-detail', {})
        return detail.get('musicInfo') or None

    async def videos(self, count=30, offset=0, prefer_scraping=False, **kwargs) -> Iterator[Video]:
        """Returns Video objects of videos created with this sound.

        - Parameters:
            - count (int): The amount of videos you want returned.
            - offset (int): The offset of videos from 0 you want to get.
            - prefer_scraping (bool): If True, get videos by scrolling the browser page
              rather than through the make_request API. Normally unnecessary: API
              requests replay the per-endpoint param template captured from the
              webapp's own music/item_list requests — lazily filled by the page
              load below on the first sound of a session (see
              ZendriverTikTokApi.cache_api_params) — changing only musicID and
              cursor. Kept as an explicit escape hatch to force the scrolling path.

        Example Usage
        ```py
        async for video in api.sound(id='7016547803243022337').videos():
            # do something
        ```
        """
        self.__ensure_valid()

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
                    "API path returned no sound videos. Falling back to scraping method."
                )
            except ApiFailedException as ex:
                self.parent.logger.warning(
                    f"API sound videos request failed: {ex}. Falling back to scraping method."
                )

        # Scraping route. Loading the sound page fires the webapp's own
        # music/item_list request, which fills the param template for that
        # endpoint (PyTok._on_request_will_be_sent -> cache_api_params). So we
        # harvest that first page off the wire and then resume paginating through
        # the API from its cursor, instead of scrolling for every page.
        await self._load_sound_page()
        items, has_more, cursor = await self._harvest_page_videos()
        self.parent.logger.info(
            f"Got {len(items)} videos from the sound page, hasMore={has_more}, cursor={cursor}"
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
                    f"API still failing after the sound page load ({ex}). "
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
                "musicID": self.id,
                "cursor": cursor,
            }

            try:
                res = await self.parent.tiktok_api.make_request(
                    url="https://www.tiktok.com/api/music/item_list/",
                    params=params,
                )
            except Exception as e:
                raise ApiFailedException(f"API request failed: {e}")

            if res is None:
                raise ApiFailedException("API returned None response")

            if res.get('type') == 'verify':
                raise ApiFailedException("TikTok API is asking for verification")

            # A non-zero status here means the replayed params were rejected (no
            # template yet, or a stale/burned one — make_request drops the
            # template in that case). Fail so the caller falls back to the page
            # load, which re-captures a fresh template off the wire.
            status_code = res.get('statusCode', 0)
            if status_code != 0:
                raise ApiFailedException(
                    f"TikTok returned error for sound videos: statusCode={status_code}"
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

    @property
    def _page_url(self):
        """The sound page URL.

        TikTok's canonical form is /music/<title-slug>-<id>, but the slug is
        cosmetic: any slug redirects to the canonical URL for that id, so a
        placeholder is fine when we don't know the title yet.
        """
        slug = re.sub(r'[^a-zA-Z0-9]+', '-', self.title or '').strip('-').lower()
        return f"https://www.tiktok.com/music/{slug or 'x'}-{self.id}"

    async def _navigate_to_sound_page(self):
        """Load the sound page.

        The page load fires the webapp's own music/detail and music/item_list
        requests, which fill the param templates for those endpoints
        (PyTok._on_request_will_be_sent -> cache_api_params).
        """
        page = self.parent._page

        # Drop anything captured for earlier operations first, so what we harvest
        # after the navigation belongs to this sound.
        await self.parent.process_pending_responses()

        url = self._page_url
        self.parent.logger.debug(f"Loading page: {url}")
        await page.send(cdp.page.navigate(url))
        async with asyncio.timeout(30):
            await page.wait_for_ready_state(until='complete', timeout=31)
        await asyncio.sleep(3)

    async def _load_sound_page(self):
        """Make sure the sound page is loaded, ready to be harvested."""
        # The page we navigate to redirects to the canonical slug for this id, so
        # compare on the id rather than on our own (possibly placeholder) slug.
        path = urlparse(self.parent._page.url or '').path.rstrip('/')
        if path.startswith('/music/') and path.endswith(f"-{self.id}"):
            # info() already scraped this page and its item_list responses are
            # still queued — reloading would throw them away.
            self.parent.logger.debug("Already on the sound page, not reloading")
            return

        await self._navigate_to_sound_page()
        await self.check_and_wait_for_captcha()
        await self.check_and_close_signin()
        if not await self._is_selector_visible(MUSIC_ITEM_SELECTOR):
            self.parent.logger.warning(
                "Sound video grid not visible yet (TikTok requires login for this "
                "feed; pass a logged-in user_data_dir if you get no results)."
            )

    async def _harvest_page_videos(self):
        """Read the item_list responses the loaded sound page fired itself.

        Keeps nudging the page with a scroll until we have both videos and a
        captured param template for the endpoint: the request TikTok fires on page
        load doesn't always carry the full param block, and without a template the
        API route can't take over. Returns (items, has_more, cursor) so pagination
        continues from where the page's own requests left off.
        """
        items = []
        has_more = True
        cursor = 0
        seen_ids = set()
        attempts = 5

        for attempt in range(attempts):
            responses = await self.parent.process_pending_responses("api/music/item_list")
            # Prefer responses for this sound, but don't discard everything if
            # TikTok changes how the request identifies the sound.
            for_this_sound = [
                resp for resp in responses
                if f"musicID={self.id}" in resp.get('url', '')
            ]
            for resp in for_this_sound or responses:
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
            await self.scroll_feed(MUSIC_ITEM_SELECTOR)
            await asyncio.sleep(2.5)

        return items, has_more, cursor

    def _has_api_template(self):
        """True once the webapp has been seen issuing a music/item_list request,
        i.e. the API route has params to replay."""
        return self.parent.tiktok_api.get_cached_api_params(
            "https://www.tiktok.com/api/music/item_list/"
        ) is not None

    def _parse_response(self, resp):
        """Decode a captured music response body, or None if unusable."""
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
        """Scroll the loaded sound page, yielding videos from each new response."""
        yielded = 0
        walk = self.scroll_walk("api/music/item_list", MUSIC_ITEM_SELECTOR,
                                before_round=self.check_and_wait_for_captcha)
        try:
            async for rnd in walk.rounds():
                for resp in rnd.responses:
                    res = self._parse_response(resp)
                    if res is None:
                        walk.stats['unusable_responses'] += 1
                        continue

                    videos = res.get("itemList", [])
                    rnd.produced += len(videos)
                    for video in videos:
                        yielded += 1
                        yield self.parent.video(data=video)

                    if 'hasMore' in res:
                        rnd.stop = not res['hasMore']
        finally:
            self.parent.logger.info(
                f"sound {self.id}: walked {yielded} videos, {walk.summary()}"
            )

    def __extract_from_data(self):
        data = self.as_dict or {}

        # Two shapes reach here: the `music` dict hanging off a video item, and
        # the musicInfo wrapper from music/detail ({music, author, stats}).
        music = data.get("music") if isinstance(data.get("music"), dict) else data

        if music.get("id") is not None:
            self.id = str(music["id"])
        self.title = music.get("title") or self.title

        # music/detail carries the real account behind an original sound in
        # `author`; `authorName` is only a display name (an artist's stage name
        # for commercial sounds, a nickname for original ones), so prefer the
        # handle when TikTok gives us one.
        author_name = (data.get("author") or {}).get("uniqueId") or music.get("authorName")
        if author_name and self.parent is not None:
            self.author = self.parent.user(username=author_name)

        if self.id is None:
            self.parent.logger.error(
                f"Failed to create Sound with data: {data}\nwhich has keys {data.keys()}"
            )

    def __ensure_valid(self):
        if not self.id:
            raise SoundRemovedException("This sound has been removed!")

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return f"PyTok.sound(id='{self.id}')"
