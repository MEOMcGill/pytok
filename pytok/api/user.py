from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Iterator, Optional

from zendriver import cdp

from ..exceptions import *
from ..helpers import extract_tag_contents

if TYPE_CHECKING:
    from ..tiktok import PyTok
    from .video import Video

from .base import Base


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

    async def _info_full_scrape(self, **kwargs) -> dict:
        url = f"https://www.tiktok.com/@{self.username}"

        page = self.parent._page

        self.parent.logger.debug(f"Loading page: {url}")
        await page.send(cdp.page.navigate(url))
        self.parent.logger.debug(f"Navigate sent, waiting for ready state")
        async with asyncio.timeout(10):
            await page.wait_for_ready_state(until='complete', timeout=11)
        await asyncio.sleep(3)  # Brief wait for dynamic content

        # Wait for video items using base class method (handles refresh button, captcha, login popup)
        await self.wait_for_content_or_unavailable_or_captcha(
            '[data-e2e="user-post-item"]',
            "Couldn't find this account",
            no_content_text=["No content", "This account is private", "Log in to TikTok"]
        )

        # Get user info from page HTML (like the working example)
        html_body = await page.get_content()
        tag_contents = extract_tag_contents(html_body)

        if not tag_contents:
            raise InvalidJSONException("Could not find data script tag in page")

        self.initial_json = json.loads(tag_contents)

        user = None
        sec_uid = None

        # Try different JSON structures TikTok uses (matching the working example)
        if '__DEFAULT_SCOPE__' in self.initial_json:
            user_detail = self.initial_json['__DEFAULT_SCOPE__'].get('webapp.user-detail', {})
            if user_detail.get('statusCode') == 0:
                user_info = user_detail.get('userInfo', {})
                user = {**user_info.get('user', {}), **user_info.get('stats', {})}
                sec_uid = user_info.get('user', {}).get('secUid')

        if 'UserModule' in self.initial_json and user is None:
            users = self.initial_json['UserModule'].get('users', {})
            stats = self.initial_json['UserModule'].get('stats', {})
            if self.username in users:
                user = {**users[self.username], **stats.get(self.username, {})}
                sec_uid = user.get('secUid')

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
            - get_bytes (bool): If True, download each video's MP4 as it is yielded and store
              it on the yielded Video as ``video.video_bytes`` (None if the download failed).
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
            if get_bytes:
                try:
                    video.video_bytes = await video.bytes()
                except Exception as ex:
                    self.parent.logger.warning(f"get_bytes: failed to download bytes for video {video.id}: {ex}")
                    video.video_bytes = None
            yield video

    async def _iter_videos(self, count=None, batch_size=100, prefer_scraping=False, **kwargs) -> Iterator[Video]:
        if self.as_dict and self.as_dict.get('videoCount', 1) == 0:
            return

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
                return

            cursor = res.get('cursor', cursor)
            await self.parent.request_delay()
        

    async def _get_videos_scraping(self, count):
        page = self.parent._page

        url = f"https://www.tiktok.com/@{self.username}"
        self.parent.logger.debug(f"Loading page: {url}")
        await page.send(cdp.page.navigate(url))
        self.parent.logger.debug(f"Navigate sent, waiting for ready state")
        async with asyncio.timeout(30):
            await page.wait_for_ready_state(until='complete', timeout=31)
        await asyncio.sleep(3)  # Brief wait for dynamic content
        self.parent.logger.debug(f"Page loaded for scraping videos")

        # Process any pending responses
        await self.parent.process_pending_responses()

        # Wait for video items using base class method (handles refresh button, captcha, login popup)
        await self.wait_for_content_or_unavailable_or_captcha(
            '[data-e2e="user-post-item"]',
            "Couldn't find this account",
            no_content_text=["No content", "This account is private", "Log in to TikTok"]
        )

        # Get initial videos from page HTML (like the working example)
        videos = []
        seen_ids = set()
        has_more = True

        html = await page.get_content()
        tag_contents = extract_tag_contents(html)

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
            tag_contents = extract_tag_contents(html)
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
        """Scroll to load more videos using zendriver."""
        page = self.parent._page
        if seen_ids is None:
            seen_ids = set()

        has_more = True
        scroll_attempts = 0
        max_scroll_attempts = 30
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

                    has_more = data.get('hasMore', False)
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

