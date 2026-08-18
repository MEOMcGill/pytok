from __future__ import annotations

import asyncio
import inspect
import json
from typing import TYPE_CHECKING, Iterator, Optional

from zendriver import cdp

from ..exceptions import *
from ..helpers import extract_tag_contents

if TYPE_CHECKING:
    from ..tiktok import PyTok
    from .video import Video

from .base import Base

# How much of a profile's own videoCount a walk has to reach before it counts as complete.
# Slack is needed because videoCount is not a promise: it keeps counting posts the listing no
# longer serves (deleted, region-locked, moderated), so a healthy walk lands a little short.
# Only consulted when TikTok never said the listing ended -- see _iter_videos.
LISTING_MIN_COVERAGE = 0.9


class User(Base):
    """
    A TikTok User.

    Example Usage
    ```py
    user = api.user(username='therock')
    # or
    user_id = '5831967'
    sec_uid = 'MS4wLjABAAAA-VASjiXTh7wDDyXvjk10VFhMWUAoxr8bgfO1kAL1-9s'
    user = api.user(user_id=user_id, sec_uid=sec_uid)
    ```

    """

    parent: PyTok
    """The PyTok instance this object is bound to (set by api.user(...))."""

    user_id: str
    """The user ID of the user."""
    sec_uid: str
    """The sec UID of the user."""
    username: str
    """The username of the user."""
    as_dict: dict
    """The raw data associated with this user."""

    def __init__(
            self,
            username: Optional[str] = None,
            user_id: Optional[str] = None,
            sec_uid: Optional[str] = None,
            data: Optional[dict] = None,
            parent: Optional[PyTok] = None,
    ):
        """
        You must provide the username or (user_id and sec_uid) otherwise this
        will not function correctly.
        """
        self.parent = parent
        self.__update_id_sec_uid_username(user_id, sec_uid, username)
        self._used_api_for_info = False
        # the webapp's own api/user/detail response, when the page was client-rendered
        # and left nothing embedded to parse (see _harvest_user_detail_xhr)
        self._xhr_user_detail: Optional[dict] = None
        if data is not None:
            self.as_dict = data
            self.__extract_from_data()
        else:
            self.as_dict = {}

    def info(self, **kwargs):
        """
        Returns a dictionary of TikTok's User object

        Example Usage
        ```py
        user_data = api.user(username='therock').info()
        ```
        """
        return self.info_full(**kwargs)

    async def info_full(self, **kwargs) -> dict:
        """
        Returns a dictionary of information associated with this User.
        Includes statistics about this user.

        Example Usage
        ```py
        user_data = api.user(username='therock').info_full()
        ```
        """

        # TODO: Find the one using only user_id & sec_uid
        if not self.username:
            raise TypeError(
                "You must provide the username when creating this class to use this method."
            )

        try:
            return await self._info_full_api(**kwargs)
        except ApiFailedException as ex:
            self.parent.logger.warning(f"TikTok-Api user.info_full() failed: {ex}. Falling back to scraping method.")
            self._used_api_for_info = False
            return await self._info_full_scrape(**kwargs)

    async def _info_full_api(self, **kwargs) -> dict:
        # Call TikTok API directly instead of using TikTok-Api's user.info()
        # to handle empty/invalid responses ourselves
        url_params = {
            "secUid": self.sec_uid if self.sec_uid else "",
            "uniqueId": self.username,
        }

        try:
            resp = await self.parent.tiktok_api.make_request(
                url="https://www.tiktok.com/api/user/detail/",
                params=url_params,
                invalid_response_callback=lambda r: 'id' not in r.get('userInfo', {}).get('user', {})

            )
        except EmptyResponseException:
            raise ApiFailedException("TikTok API returned empty response")

        if resp is None:
            raise ApiFailedException("TikTok returned None response")

        status_code = max(resp.get('statusCode', 0), resp.get('status_code', 0))

        if status_code != 0:
            if status_code in (10202, 10221, 100002):
                raise NotFoundException(
                    f"TikTok indicated that this user does not exist: statusCode={status_code}"
                )
            elif status_code in (10101, 209002):
                if await self.parent._is_logged_in():
                    raise ApiFailedException()
                else:
                    raise LoginException(
                        f"TikTok requires login to view this content, log in using the login() method before accessing this user: statusCode={status_code}"
                    )
            elif status_code == 10222:
                raise AccountPrivateException(
                    f"This TikTok account is private and cannot be scraped: statusCode={status_code}"
                )
            else:
                raise ApiFailedException(
                    f"TikTok returned error for user info: statusCode={status_code}"
                )

        # Check if we got valid user data
        user_info = resp.get("userInfo", {})
        user_data = user_info.get("user", {})

        if not user_data or not user_data.get("id"):
            raise ApiFailedException("TikTok API returned invalid user data")

        self.as_dict = resp
        self.__extract_from_data()
        self._used_api_for_info = True
        return resp

    @staticmethod
    def _user_from_user_detail(data) -> Optional[dict]:
        """Pull the user out of an api/user/detail payload.

        The rehydration tag's webapp.user-detail scope carries the same shape, so both
        delivery routes parse through here.
        """
        if not isinstance(data, dict) or data.get('statusCode', 0) != 0:
            return None
        user_info = data.get('userInfo', {})
        user = user_info.get('user', {})
        if not user or not user.get('id'):
            return None
        return {**user, **user_info.get('stats', {})}

    async def _harvest_user_detail_xhr(self) -> bool:
        """Bank the webapp's own api/user/detail response, if it made one.

        A server-rendered page never requests that endpoint — its details are already
        embedded — so this only ever finds anything on a client-rendered one, where the
        captured response is the *only* copy of the user's details in existence.

        Reading the capture drains it, hence stashing the first hit: the caller polls
        this, and the response must survive until the parse.
        """
        if self._xhr_user_detail is not None:
            return True
        try:
            responses = await self.parent.process_pending_responses('api/user/detail')
        except Exception:
            return False
        for resp in responses or []:
            body = resp.get('body') or ''
            if not body:
                continue
            try:
                data = json.loads(body) if isinstance(body, str) else body
            except Exception:
                continue
            user = self._user_from_user_detail(data)
            if not user:
                continue
            # the page may have fetched details for other users too (sidebars, suggested
            # accounts), so don't accept one that isn't the profile we asked for
            unique_id = user.get('uniqueId')
            if unique_id and unique_id.lower() != self.username.lower():
                continue
            self._xhr_user_detail = user
            return True
        return False

    async def _info_full_scrape(self, **kwargs) -> dict:
        url = f"https://www.tiktok.com/@{self.username}"

        page = self.parent._page

        self.parent.logger.debug(f"Loading page: {url}")
        await page.send(cdp.page.navigate(url))
        self.parent.logger.debug(f"Navigate sent, waiting for the profile's data")
        # Wait for the data rather than for readyState 'complete': the tag is parseable
        # long before the page finishes pulling in its media, and on a heavy profile
        # 'complete' may never arrive. Either delivery route ends the wait -- embedded in
        # the tag, or fetched by the page from api/user/detail.
        self._xhr_user_detail = None
        await self._wait_for_page_data(page, f"@{self.username}",
                                       fallback=self._harvest_user_detail_xhr)

        # Wait for video items using base class method (handles refresh button, captcha, login popup)
        await self.wait_for_content_or_unavailable_or_captcha(
            '[data-e2e="user-post-item"]',
            "Couldn't find this account",
            no_content_text=["No content", "This account is private", "Log in to TikTok"]
        )
        # The grid's own listing response has landed by now, and nothing reads it until the
        # caller asks for videos -- which may be a captcha solve and a page of work away.
        # Bank it here so it is ours rather than Chrome's for that whole gap.
        await self.parent.collect_pending_response_bodies()

        user = None

        # Get user info from page HTML (like the working example)
        html_body = await page.get_content()
        try:
            tag_contents = extract_tag_contents(html_body)
        except NotAvailableException:
            # No embedded payload at all, which is a client-rendered page rather than a
            # missing account -- the XHR route below is where its details are. Letting
            # this out would report the profile as unavailable, which the accounts pool
            # treats as data-level and skips the handle for good.
            tag_contents = None

        if tag_contents:
            self.initial_json = json.loads(tag_contents)

            # Try different JSON structures TikTok uses (matching the working example)
            if '__DEFAULT_SCOPE__' in self.initial_json:
                user = self._user_from_user_detail(
                    self.initial_json['__DEFAULT_SCOPE__'].get('webapp.user-detail', {})
                )

            if 'UserModule' in self.initial_json and user is None:
                users = self.initial_json['UserModule'].get('users', {})
                stats = self.initial_json['UserModule'].get('stats', {})
                if self.username in users:
                    user = {**users[self.username], **stats.get(self.username, {})}

        if user is None:
            # Client-rendered: nothing embedded to parse, so use what the page fetched
            # for itself. Re-harvest first — the response may only have landed while we
            # were waiting on the video grid above.
            await self._harvest_user_detail_xhr()
            user = self._xhr_user_detail
            if user is not None:
                self.parent.logger.debug(
                    f"@{self.username}: page carried no embedded data, took the user's "
                    f"details from its own api/user/detail response"
                )

        if user is None:
            raise InvalidJSONException("Failed to find user data in HTML")

        self.as_dict = user
        self.__extract_from_data()
        return user

    async def videos(self, get_bytes=False, count=None, batch_size=100, prefer_scraping=False, **kwargs) -> Iterator[Video]:
        """
        Returns an iterator yielding Video objects.

        - Parameters:
            - count (int): The amount of videos you want returned.
            - get_bytes (bool | callable): If True, download each video's MP4 as it is yielded
              and store it on the yielded Video as ``video.video_bytes`` (None if the download
              failed). May instead be a predicate ``(video) -> bool`` (sync or async, and given
              the video before its bytes are fetched, so ``video.id``/``video.as_dict`` are
              available), consulted per video: when it returns False no download is attempted
              and ``video.video_bytes`` stays None. Use it to skip videos you already hold
              somewhere, since the download happens in here rather than in the caller's loop.
            - prefer_scraping (bool): If True, get videos by scraping the browser page rather
              than the make_request API. Normally unnecessary: API requests reuse a per-endpoint
              param template captured from the webapp's own requests (lazily filled by the
              scraping route on the first request of each endpoint type — see
              ZendriverTikTokApi.cache_api_params), so their playAddr URLs download cleanly in
              ``video.bytes()``. Kept as an explicit escape hatch to force the scraping path.
            - cursor (int): The unix epoch to get uploaded videos since.

        Example Usage
        ```py
        user = api.user(username='therock')
        async for video in user.videos(count=100, get_bytes=True):
            with open(f"{video.id}.mp4", "wb") as f:
                f.write(video.video_bytes)
        ```
        """
        async for video in self._iter_videos(count=count, batch_size=batch_size, prefer_scraping=prefer_scraping, **kwargs):
            video.video_bytes = None
            if get_bytes:
                wanted = True
                if callable(get_bytes):
                    try:
                        wanted = get_bytes(video)
                        if inspect.isawaitable(wanted):
                            wanted = await wanted
                    except Exception as ex:
                        # a broken predicate shouldn't silently turn into "download everything"
                        self.parent.logger.warning(f"get_bytes: predicate failed for video {video.id}, skipping bytes: {ex}")
                        wanted = False
                if wanted:
                    try:
                        video.video_bytes = await video.bytes()
                    except Exception as ex:
                        self.parent.logger.warning(f"get_bytes: failed to download bytes for video {video.id}: {ex}")
                        video.video_bytes = None
            yield video

    async def _iter_videos(self, count=None, batch_size=100, prefer_scraping=False,
                           min_coverage=None, **kwargs) -> Iterator[Video]:
        """Yield this user's videos, refusing to pass off a blocked listing as an empty one.

        Every route in here (initial page harvest, API, scraping) falls back to the next on
        failure, and the last one just stops if it finds nothing -- so a session TikTok is
        bot-blocking produced an empty iterator that a caller could not tell apart from an
        account with no videos. That silence is expensive downstream: the TikTok crawler
        recorded such handles as successfully collected and wrote PHH crawler history for the
        range, telling the gap report never to ask again.

        The profile's own videoCount is the discriminator, and it is what makes this safe to
        check here rather than in the caller: it compares against what this iterator *yielded*,
        not what the caller kept, so a caller filtering everything out (a date window, say) is
        not mistaken for a failure. Raised as ApiFailedException because a block is
        session-level -- the accounts WorkerPool rotates and retries on a fresh session.

        A walk cut short *after* yielding something was equally silent, and is the common
        case: a bot-flagged session still gets the first page, which comes from the profile
        HTML rather than from the item_list endpoint, so the listing dies after a page or two
        while the guard above sees 16 videos and reports success.

        `_listing_exhausted` separates the two. It is set only where TikTok itself said
        nothing followed this page, so without it a walk far short of videoCount was cut off
        rather than finished.
        """
        # None when info() was never called: no expectation to check against, so the guard
        # below stays off rather than guessing.
        expected_videos = self.as_dict.get('videoCount') if self.as_dict else None
        if expected_videos == 0:
            return

        # "did TikTok tell us the listing ended?" -- reset per walk, since a True left by an
        # earlier walk on this object would suppress the truncation check below
        self._listing_exhausted = False

        amount_yielded = 0
        async for video in self._iter_videos_inner(count=count, batch_size=batch_size,
                                                  prefer_scraping=prefer_scraping, **kwargs):
            amount_yielded += 1
            yield video

        # Note both checks are reached only when the iterator above ran to exhaustion: a
        # caller that breaks out (the crawler stopping at a date window) leaves this
        # generator suspended at its yield, so neither guard can fire on a deliberate stop.
        if amount_yielded == 0 and expected_videos:
            raise ApiFailedException(
                f"no videos returned for @{self.username} though the profile reports "
                f"{expected_videos} -- the listing failed rather than being empty "
                f"(TikTok commonly answers a blocked session with an empty response)"
            )

        if count and amount_yielded >= count:
            return  # the caller's own cap, not a truncation
        if self._listing_exhausted or not expected_videos:
            # TikTok said the listing ended, so a shortfall is a stale videoCount, not a block
            return

        floor = expected_videos * (LISTING_MIN_COVERAGE if min_coverage is None else min_coverage)
        if amount_yielded < floor:
            raise FewerVideosThanExpectedException(
                f"listing for @{self.username} stopped after {amount_yielded} videos though "
                f"the profile reports {expected_videos}, and TikTok never said the listing "
                f"ended -- the walk was cut short rather than finished; retry on a fresh "
                f"session"
            )

    async def _iter_videos_inner(self, count=None, batch_size=100, prefer_scraping=False, **kwargs) -> Iterator[Video]:
        # If user info was obtained via TikTok-Api, use API for videos directly
        # If user info was scraped (page already loaded), get initial videos from page first
        amount_yielded = 0
        if not self._used_api_for_info:
            # Try to harvest the videos already on the loaded page. If that fails to find any
            # (ApiFailedException), fall through to the API/scraping path below. LoginException
            # and NoContentException are meaningful and intentionally propagate.
            try:
                videos, finished, cursor = await self._get_initial_videos(count)
            except ApiFailedException as ex:
                self.parent.logger.warning(f"Initial video page harvest failed: {ex}. Falling back to API/scraping method.")
            else:
                self.parent.logger.info(f"Got {len(videos)} initial videos, finished={finished}, cursor={cursor}")
                for video in videos:
                    yield video
                    amount_yielded += 1
                    if count and amount_yielded >= count:
                        self.parent.logger.info(f"Reached count limit after {amount_yielded} initial videos")
                        return

                if finished:
                    self.parent.logger.info(f"Finished after initial videos")
                    # the page's own item_list responses carried hasMore=false
                    self._listing_exhausted = True
                    return

                self.parent.logger.info(f"Continuing with _get_videos_api to get more videos")

        remaining = None if count is None else count - amount_yielded
        if prefer_scraping:
            # Explicit opt-out of the API path: go straight to scraping for
            # browser-sourced URLs (see the prefer_scraping docstring above).
            async for video in self._get_videos_scraping(remaining):
                yield video
            return
        try:
            async for video in self._get_videos_api(count=remaining, cursor=0, **kwargs):
                yield video
        except ApiFailedException as ex:
            self.parent.logger.warning(f"API method failed with exception: {ex}. Falling back to scraping method.")
            async for video in self._get_videos_scraping(remaining):
                yield video


    async def _get_videos_api(self, count=None, cursor=0, **kwargs) -> Iterator[Video]:
        # Use TikTok-Api's make_request method instead of manual requests
        self.parent.logger.debug(f"Starting _get_videos_api with cursor={cursor}, count={count}")
        amount_yielded = 0

        while (count is None or amount_yielded < count):
            params = {
                'secUid': self.sec_uid,
                'count': 16,  # match the frontend, which paginates item_list at 16
                'cursor': cursor,
                'coverFormat': 2,  # Browser sends this parameter
            }

            self.parent.logger.debug(f"Making TikTok-Api request with cursor={cursor}")
            # Use TikTok-Api's make_request which handles signing and headers
            try:
                res = await self.parent.tiktok_api.make_request(
                    url="https://www.tiktok.com/api/post/item_list/",
                    params=params,
                )
            except Exception as e:
                # Convert any exception from make_request to ApiFailedException
                # to trigger fallback to scraping method
                self.parent.logger.warning(f"make_request failed: {e}")
                raise ApiFailedException(f"TikTok-Api make_request failed: {e}")
            self.parent.logger.debug(f"TikTok-Api response received with {len(res.get('itemList', []))} videos")

            if res is None:
                raise ApiFailedException("TikTok-Api returned None response")

            if res.get('type') == 'verify':
                raise ApiFailedException("TikTok API is asking for verification")

            # Check for error status codes indicating videos can't be loaded
            status_code = res.get('statusCode', 0)
            if status_code != 0:
                status_msg = res.get('statusMsg', 'Unknown error')
                if status_code in (10101, 209002):
                    if await self.parent._is_logged_in():
                        raise ApiFailedException("TikTok-Api cannot currently use logged in session to access this content")
                    else:
                        raise LoginException(
                            f"TikTok requires login to view this content: statusCode={status_code}"
                        )
                raise NoContentException(
                    f"TikTok returned error for user videos: statusCode={status_code}, statusMsg={status_msg}"
                )

            videos = res.get('itemList', [])

            for video in videos:
                yield self.parent.video(data=video)
                amount_yielded += 1
                if count is not None and amount_yielded >= count:
                    return

            has_more = res.get("hasMore")
            if not has_more:
                self.parent.logger.info(
                    "TikTok isn't sending more TikToks beyond this point."
                )
                # reached the documented end of the listing, so the walk is complete
                self._listing_exhausted = True
                return

            cursor = res.get('cursor', cursor)
            await self.parent.request_delay()
        

    async def _get_videos_scraping(self, count):
        page = self.parent._page

        url = f"https://www.tiktok.com/@{self.username}"
        self.parent.logger.debug(f"Loading page: {url}")
        await page.send(cdp.page.navigate(url))
        self.parent.logger.debug(f"Navigate sent, waiting for ready state")
        # Not fatal if 'complete' never lands: the wait for the video grid below is the
        # real readiness gate, and a profile heavy enough to never finish loading is
        # still perfectly scrapeable once its grid is up.
        await self._wait_for_page_load(page, f"@{self.username}", required=False)
        await asyncio.sleep(3)  # Brief wait for dynamic content
        self.parent.logger.debug(f"Page loaded for scraping videos")

        # Bank what the page has fetched so far. Draining it instead would throw away this
        # page's own listing response, which on a profile whose grid fits in one page is
        # the only one there will ever be -- nothing further is lazy-loaded for the scroll
        # below to pick up.
        await self.parent.collect_pending_response_bodies()

        # Wait for video items using base class method (handles refresh button, captcha, login popup)
        await self.wait_for_content_or_unavailable_or_captcha(
            '[data-e2e="user-post-item"]',
            "Couldn't find this account",
            no_content_text=["No content", "This account is private", "Log in to TikTok"]
        )
        await self.parent.collect_pending_response_bodies()

        # Get initial videos from page HTML (like the working example)
        videos = []
        seen_ids = set()
        has_more = True

        html = await page.get_content()
        try:
            tag_contents = extract_tag_contents(html)
        except NotAvailableException:
            # A client-rendered page embeds nothing, so there is no first page to read
            # here -- but the scroll below works off the item_list responses the page
            # fetches for itself, which arrive either way. has_more stays True so we go
            # straight there rather than reporting the profile as unavailable.
            tag_contents = None

        if tag_contents:
            data = json.loads(tag_contents)

            if '__DEFAULT_SCOPE__' in data:
                post_data = data['__DEFAULT_SCOPE__'].get('webapp.user-post', {})

                # Check for error status codes indicating videos can't be loaded
                status_code = post_data.get('statusCode', 0)
                if status_code != 0:
                    status_msg = post_data.get('statusMsg', 'Unknown error')
                    if status_code in (10101, 209002):
                        if await self.parent._is_logged_in():
                            raise ApiFailedException()
                        else:
                            raise LoginException(
                                f"TikTok requires login to view this content: statusCode={status_code}"
                            )
                    raise NoContentException(
                        f"TikTok returned error for user videos: statusCode={status_code}, statusMsg={status_msg}"
                    )

                item_list = post_data.get('itemList', [])
                for item in item_list:
                    video_id = item.get('id')
                    if video_id and video_id not in seen_ids:
                        videos.append(item)
                        seen_ids.add(video_id)
                has_more = post_data.get('hasMore', True)

            elif 'ItemModule' in data:
                items = data.get('ItemModule', {})
                for item_id, item in items.items():
                    if item_id not in seen_ids:
                        videos.append(item)
                        seen_ids.add(item_id)

        self.parent.logger.info(f"Got {len(videos)} videos from initial page")

        # Yield initial videos
        yielded = 0
        for video in videos:
            yield self.parent.video(data=video)
            yielded += 1
            if count and yielded >= count:
                return

        if not has_more:
            # the embedded page JSON says this first page is the whole profile
            self._listing_exhausted = True
            return

        # Scroll to get more videos
        async for video in self._get_videos_scroll(count, seen_ids, yielded):
            yield video

    async def _get_initial_videos(self, count):
        self.parent.logger.debug("Getting initial videos from page responses")
        all_videos = []
        finished = False

        cursor = 0
        # Process pending responses for video list API using CDP
        video_responses = await self.parent.process_pending_responses('api/post/item_list')
        video_responses = [res for res in video_responses if f"secUid={self.sec_uid}" in res.get('url', '')]
        self.parent.logger.debug(f"Found {len(video_responses)} video responses in page")

        for video_response in video_responses:
            try:
                body = video_response.get('body', '')
                if not body:
                    continue
                video_data = json.loads(body) if isinstance(body, str) else body

                # Check for error status codes
                status_code = video_data.get('statusCode', 0)
                if status_code != 0:
                    status_msg = video_data.get('statusMsg', 'Unknown error')
                    if status_code in (10101, 209002):
                        if await self.parent._is_logged_in():
                            raise ApiFailedException()
                        else:
                            raise LoginException(
                                f"TikTok requires login to view this content: statusCode={status_code}"
                            )
                    raise NoContentException(
                        f"TikTok returned error for user videos: statusCode={status_code}, statusMsg={status_msg}"
                    )

                if video_data.get('itemList'):
                    videos = video_data['itemList']
                    video_objs = [self.parent.video(data=video) for video in videos]
                    all_videos += video_objs
                finished = not video_data.get('hasMore', False)
                cursor = video_data.get('cursor', 0)
            except (NoContentException, LoginException):
                raise
            except Exception as ex:
                self.parent.logger.debug(f"Error processing video response: {ex}")

        if len(video_responses) == 0:
            # Check HTML data for status codes before failing
            html = await self.parent._page.get_content()
            try:
                tag_contents = extract_tag_contents(html)
            except NotAvailableException:
                # Best-effort status check only; a client-rendered page has no embedded
                # JSON to check. Fall through to ApiFailedException, which retries and
                # falls back, rather than letting "no tag" surface as "no such account".
                tag_contents = None
            if tag_contents:
                data = json.loads(tag_contents)
                if '__DEFAULT_SCOPE__' in data:
                    post_data = data['__DEFAULT_SCOPE__'].get('webapp.user-post', {})
                    status_code = post_data.get('statusCode', 0)
                    if status_code in (10101, 209002):
                        if await self.parent._is_logged_in():
                            raise ApiFailedException()
                        else:
                            raise LoginException(
                                f"TikTok requires login to view this content: statusCode={status_code}"
                            )
            raise ApiFailedException("Failed to get videos from API")

        self.parent.request_cache['videos'] = video_responses[-1]
        return all_videos, finished, cursor

    async def _get_videos_scroll(self, count, seen_ids=None, amount_yielded=0):
        """Scroll to load more videos using zendriver.

        Most walks end up here, because item_list is the route TikTok blocks first, so how
        this loop decides to stop sets how deep a walk can get. Two of its stopping
        conditions used to be indistinguishable from reaching the end of a profile; only this
        loop knows which one fired, so it records that in `_listing_exhausted`.
        """
        page = self.parent._page
        if seen_ids is None:
            seen_ids = set()

        has_more = True
        scroll_attempts = 0
        # A bounded walk stops at `count` anyway; an unbounded one wants the whole profile,
        # which 30 scrolls caps at a few hundred videos. The no-new-videos check below is the
        # real terminator, so this is only a backstop against scrolling a page that never ends.
        max_scroll_attempts = 30 if count else 400
        no_new_videos_count = 0
        last_video_count = amount_yielded

        while scroll_attempts < max_scroll_attempts and has_more:
            # Scroll down
            await page.evaluate('window.scrollBy(0, window.innerHeight * 3)')
            await asyncio.sleep(2)

            # Check for refresh button that may appear during scrolling
            await self.check_and_resolve_refresh_button()

            # Process any pending responses
            video_responses = await self.parent.process_pending_responses('api/post/item_list')

            for resp in video_responses:
                body = resp.get('body', '')
                if not body:
                    continue

                try:
                    data = json.loads(body) if isinstance(body, str) else body
                    item_list = data.get('itemList', [])
                    for item in item_list:
                        video_id = item.get('id')
                        if video_id and video_id not in seen_ids:
                            seen_ids.add(video_id)
                            amount_yielded += 1
                            yield self.parent.video(data=item)

                            if count and amount_yielded >= count:
                                return

                    # Only believe hasMore when TikTok actually sent it. Defaulting to False
                    # ended the walk on any response arriving without it -- which is what an
                    # empty bot-blocked body looks like.
                    if 'hasMore' in data:
                        has_more = data['hasMore']
                except Exception as e:
                    self.parent.logger.debug(f"Error processing video response: {e}")

            current_count = amount_yielded
            if current_count == last_video_count:
                no_new_videos_count += 1
                if no_new_videos_count >= 5:
                    self.parent.logger.info("No new videos found after multiple scrolls, stopping")
                    break
            else:
                no_new_videos_count = 0

            last_video_count = current_count
            scroll_attempts += 1

        # Only a hasMore=false answer means the profile ran out. Giving up because the page
        # stopped producing new videos, or because the backstop above ran out of scrolls, is a
        # walk that was cut off -- _iter_videos raises on that so the pool retries the handle
        # on a fresh session instead of recording a partial profile as fully collected.
        if not has_more:
            self._listing_exhausted = True
        else:
            self.parent.logger.info(
                f"Stopped scrolling @{self.username} after {scroll_attempts} scrolls with "
                f"{amount_yielded} videos while TikTok still reported more available"
            )

    def liked(self, count: int = 30, cursor: int = 0, **kwargs) -> Iterator[Video]:
        """
        Returns a dictionary listing TikToks that a given a user has liked.

        **Note**: The user's likes must be **public** (which is not the default option)

        - Parameters:
            - count (int): The amount of videos you want returned.
            - cursor (int): The unix epoch to get uploaded videos since.

        Example Usage
        ```py
        for liked_video in api.user(username='public_likes'):
            # do something
        ```
        """
        processed = self.parent._process_kwargs(kwargs)
        kwargs["custom_device_id"] = processed.device_id

        amount_yielded = 0
        first = True

        if self.user_id is None and self.sec_uid is None:
            self.__find_attributes()

        while amount_yielded < count:
            query = {
                "count": 30,
                "id": self.user_id,
                "type": 2,
                "secUid": self.sec_uid,
                "cursor": cursor,
                "sourceType": 9,
                "appId": 1233,
                "region": processed.region,
                "priority_region": processed.region,
                "language": processed.language,
            }
            path = "api/favorite/item_list/?{}&{}".format(
                self.parent._add_url_params(), urlencode(query)
            )

            res = self.parent.get_data(path, **kwargs)

            if "itemList" not in res.keys():
                if first:
                    self.parent.logger.error("User's likes are most likely private")
                return

            videos = res.get("itemList", [])
            amount_yielded += len(videos)
            for video in videos:
                amount_yielded += 1
                yield self.parent.video(data=video)

            if not res.get("hasMore", False) and not first:
                self.parent.logger.info(
                    "TikTok isn't sending more TikToks beyond this point."
                )
                return

            cursor = res["cursor"]
            first = False

    def __extract_from_data(self):
        data = self.as_dict
        keys = data.keys()

        if "userInfo" in keys:
            user_info = data["userInfo"]
            # TikTok-Api returns data in userInfo.user structure
            if "user" in user_info:
                user = user_info["user"]
                self.__update_id_sec_uid_username(
                    user.get("id"),
                    user.get("secUid"),
                    user.get("uniqueId"),
                )
            else:
                # Legacy format
                self.__update_id_sec_uid_username(
                    user_info.get("uid"),
                    user_info.get("sec_uid"),
                    user_info.get("unique_id"),
                )
        elif "uniqueId" in keys:
            self.__update_id_sec_uid_username(
                data["id"], data["secUid"], data["uniqueId"]
            )

        if None in (self.username, self.user_id, self.sec_uid):
            self.parent.logger.error(
                f"Failed to create User with data: {data}\nwhich has keys {data.keys()}"
            )

    def __update_id_sec_uid_username(self, id, sec_uid, username):
        self.user_id = id
        self.sec_uid = sec_uid
        self.username = username

    def __find_attributes(self) -> None:
        # It is more efficient to check search first, since self.user_object() makes HTML request.
        found = False
        for u in self.parent.search(self.username).users():
            if u.username == self.username:
                found = True
                self.__update_id_sec_uid_username(u.user_id, u.sec_uid, u.username)
                break

        if not found:
            user_object = self.info()
            self.__update_id_sec_uid_username(
                user_object["id"], user_object["secUid"], user_object["uniqueId"]
            )

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return f"PyTok.user(username='{self.username}', user_id='{self.user_id}', sec_uid='{self.sec_uid}')"

