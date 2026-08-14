from __future__ import annotations

import asyncio
import json
import re

from typing import TYPE_CHECKING, Iterator, Optional
from urllib.parse import parse_qs, urlparse

from zendriver import cdp

if TYPE_CHECKING:
    from ..tiktok import PyTok
    from .video import Video

from .base import Base
from ..exceptions import *
from ..helpers import extract_tag_contents

# challenge/detail statusCode TikTok returns for a hashtag that doesn't exist.
HASHTAG_NOT_FOUND_STATUS_CODES = (10205,)

# What the hashtag page renders instead of a video grid when the tag doesn't
# exist. Apostrophes are normalised before matching, since TikTok renders a
# typographic apostrophe rather than the ASCII one.
HASHTAG_NOT_FOUND_TEXTS = ("Couldn't find this hashtag",)


def _apostrophe_variants(texts):
    """Both apostrophe spellings of each text, for matchers that compare literally."""
    variants = []
    for text in texts:
        variants.append(text)
        if "'" in text:
            variants.append(text.replace("'", "’"))
    return variants


# The hashtag feed's items, used both to wait for the feed and to report how far it
# has grown during a scroll walk.
CHALLENGE_ITEM_SELECTOR = '[data-e2e=challenge-item]'


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

        status_code = max(resp.get('statusCode', 0), resp.get('status_code', 0))
        if status_code in HASHTAG_NOT_FOUND_STATUS_CODES:
            raise NoContentException(
                f"TikTok has no hashtag '{self.name}': statusCode={status_code}"
            )
        if status_code != 0:
            raise ApiFailedException(
                f"TikTok returned error for hashtag info: statusCode={status_code}"
            )

        if 'challengeInfo' not in resp:
            raise ApiFailedException("Failed to get challengeInfo from response")

        challenge_info = resp['challengeInfo']
        # A statusCode==0 response for an unknown tag can still come back with a
        # hollow challengeInfo (no id) rather than a not-found status.
        if not self._challenge_info_has_id(challenge_info):
            raise NoContentException(
                f"TikTok returned no hashtag data for '{self.name}'"
            )

        self.as_dict = challenge_info
        self.__extract_from_data()
        return self.as_dict

    @staticmethod
    def _challenge_info_has_id(challenge_info):
        """True if this challengeInfo names a hashtag that actually exists."""
        if not challenge_info:
            return False
        return bool(challenge_info.get('id') or challenge_info.get('challenge', {}).get('id'))

    async def _info_full_scrape(self, **kwargs) -> dict:
        await self._navigate_to_hashtag_page()
        # Check for the not-found page before waiting on the video grid: that wait
        # spends ~90s reloading and scrolling before giving up with a
        # TimeoutException, which reads as a transient fault and gets the account
        # rotated (see accounts.worker) instead of reporting a missing hashtag.
        await self._raise_if_hashtag_missing()
        await self.check_and_wait_for_captcha()

        challenge_info, page_challenge_id = await self._scrape_challenge_info()

        try:
            await self.wait_for_content_or_unavailable_or_captcha(
                CHALLENGE_ITEM_SELECTOR,
                'Not available',
                no_content_text=_apostrophe_variants(HASHTAG_NOT_FOUND_TEXTS),
            )
        except TimeoutException:
            # The grid matters to videos(), which scrolls it for more; for info()
            # alone what we already read off the page is enough.
            if challenge_info is None and page_challenge_id is None:
                raise
            self.parent.logger.warning(
                "Hashtag video grid never rendered, but the page identified the hashtag"
            )
        await self.check_and_close_signin()

        if challenge_info is None:
            # Anonymous sessions get the same object inlined in the page.
            challenge_info = self._challenge_info_from_html(
                await self.parent._page.get_content()
            )

        if challenge_info is None and page_challenge_id is not None:
            self.parent.logger.warning(
                f"Hashtag page never fetched challenge/detail for '{self.name}'; "
                f"continuing with id {page_challenge_id} only, without its stats"
            )
            challenge_info = {'challenge': {'id': page_challenge_id, 'title': self.name}}

        if challenge_info is None:
            raise ApiFailedException("Failed to get challengeInfo from the hashtag page")

        if not self._challenge_info_has_id(challenge_info):
            raise NoContentException(
                f"The hashtag page carries no data for '{self.name}'"
            )

        self.as_dict = challenge_info
        self.__extract_from_data()
        return self.as_dict

    async def _scrape_challenge_info(self, reloads=2):
        """Get this hashtag's data off the loaded page.

        TikTok serves two variants of the hashtag page: one whose JS fetches
        challenge/detail, and one that renders the same feed from SSR and fetches
        nothing. Only the first carries the hashtag's data for a logged-in session
        (the rehydration JSON inlines a webapp.challenge-detail scope for
        anonymous sessions only), and which one you get looks like a coin flip, so
        reload to ask again. Landing the response also captures the param template
        that lets the API route serve this endpoint for the rest of the session.

        Returns (challengeInfo or None, hashtag id the page reported or None) —
        the id lets the caller carry on without the stats.
        """
        challenge_info = await self._harvest_challenge_info()
        page_challenge_id = None

        for _ in range(reloads):
            if challenge_info is not None:
                return challenge_info, None
            # Read the id before reloading: navigating clears the collected
            # responses that carry it.
            page_challenge_id = page_challenge_id or await self._find_challenge_id()
            self.parent.logger.info(
                "Hashtag page rendered without fetching challenge/detail, reloading"
            )
            await self._navigate_to_hashtag_page()
            await self._raise_if_hashtag_missing()
            await self.check_and_wait_for_captcha()
            challenge_info = await self._harvest_challenge_info()

        if challenge_info is not None:
            return challenge_info, None
        return None, page_challenge_id or await self._find_challenge_id()

    async def _find_challenge_id(self):
        """The hashtag's id as the loaded page itself reports it, or None.

        For the SSR variant of the page this is all there is: the app deep link
        in the page's meta tags, and the challengeID on the feed request the page
        did fire, both name the hashtag even though nothing fetched its details.
        """
        html = await self.parent._page.get_content()
        match = re.search(r'challenge/detail/(\d+)', html)
        if match:
            return match.group(1)

        for url in self.parent.seen_request_urls('challenge/item_list'):
            challenge_id = parse_qs(urlparse(url).query).get('challengeID', [None])[0]
            if challenge_id:
                return challenge_id
        return None

    async def _harvest_challenge_info(self, rounds=5, delay=1.5):
        """Poll the captured challenge/detail responses for this hashtag's data.

        The response can land a moment after the page reports itself loaded, so
        poll rather than reading once. Returns None if it never arrives.
        """
        for attempt in range(rounds):
            for resp in await self.parent.process_pending_responses("api/challenge/detail"):
                res = self._parse_response(resp)
                if res and res.get('challengeInfo'):
                    return res['challengeInfo']
            if attempt < rounds - 1:
                await asyncio.sleep(delay)
        return None

    async def _raise_if_hashtag_missing(self):
        """Raise NoContentException if the loaded page is TikTok's not-found page.

        TikTok serves a normal 200 page for a hashtag that doesn't exist: no video
        grid, an empty webapp.challenge-detail in the rehydration JSON, and a
        "Couldn't find this hashtag" message. Only positive evidence counts — a
        merely missing grid can also mean a login wall or a slow render.
        """
        page_text = await self._get_page_text()
        if page_text:
            normalised = page_text.replace("’", "'")
            for text in HASHTAG_NOT_FOUND_TEXTS:
                if text in normalised:
                    raise NoContentException(
                        f"TikTok has no hashtag '{self.name}': page says '{text}'"
                    )

        detail = self._challenge_detail_from_html(await self.parent._page.get_content())
        if detail is None:
            return
        status_code = max(detail.get('statusCode', 0), detail.get('status_code', 0))
        if status_code in HASHTAG_NOT_FOUND_STATUS_CODES:
            raise NoContentException(
                f"TikTok has no hashtag '{self.name}': page statusCode={status_code}"
            )

    async def _get_page_text(self):
        """The page's visible text, or None if it can't be read."""
        try:
            return await self.parent._page.evaluate('document.body.innerText')
        except Exception as ex:
            self.parent.logger.debug(f"Could not read page text: {ex}")
            return None

    def _challenge_detail_from_html(self, html):
        """The page's webapp.challenge-detail scope, or None if there isn't one."""
        tag_contents = extract_tag_contents(html)
        if not tag_contents:
            return None
        try:
            data = json.loads(tag_contents)
        except json.JSONDecodeError:
            return None
        return data.get('__DEFAULT_SCOPE__', {}).get('webapp.challenge-detail')

    def _challenge_info_from_html(self, html):
        """Pull challengeInfo out of the hashtag page's rehydration JSON."""
        detail = self._challenge_detail_from_html(html) or {}
        return detail.get('challengeInfo') or None

    async def videos(self, count=30, offset=0, prefer_scraping=False, **kwargs) -> Iterator[Video]:
        """Returns a dictionary listing TikToks with a specific hashtag.

        - Parameters:
            - count (int | None): The amount of videos you want returned. None means
              keep paginating until the hashtag listing runs out.
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

        # count=None means "keep going until the listing runs out", so ask whether
        # there is a limit at all before comparing against it.
        def enough():
            return count is not None and amount_yielded >= count

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
                if enough():
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
        if enough():
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
        await self._raise_if_hashtag_missing()
        if not await self._is_selector_visible(CHALLENGE_ITEM_SELECTOR):
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
            await self.scroll_feed(CHALLENGE_ITEM_SELECTOR)
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
        yielded = 0
        walk = self.scroll_walk("api/challenge/item_list", CHALLENGE_ITEM_SELECTOR,
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
            self.parent.logger.info(f"#{self.name}: walked {yielded} videos, {walk.summary()}")

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
