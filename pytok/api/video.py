from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import logging
import json
from urllib import parse as url_parsers
from typing import TYPE_CHECKING, Optional

import brotli
import httpx
import requests

if TYPE_CHECKING:
    from ..tiktok import PyTok
    from .user import User
    from .sound import Sound
    from .hashtag import Hashtag

from .base import Base
from ..helpers import extract_tag_contents, edit_url, extract_video_id_from_url, extract_user_id_from_url
from .. import exceptions

logger = logging.getLogger("pytok.api.video")

class Counter:
    def __init__(self):
        self._counter = 0

    def add(self, n):
        self._counter += n

    def get(self):
        return self._counter

class Video(Base):
    """
    A TikTok Video class

    Example Usage
    ```py
    video = api.video(id='7041997751718137094')
    ```
    """

    parent: PyTok
    """The PyTok instance this object is bound to (set by api.video(...))."""

    id: Optional[str]
    """TikTok's ID of the Video"""
    create_time: Optional[datetime]
    """The creation time of the Video"""
    stats: Optional[dict]
    """TikTok's stats of the Video"""
    author: Optional[User]
    """The User who created the Video"""
    sound: Optional[Sound]
    """The Sound that is associated with the Video"""
    hashtags: Optional[list[Hashtag]]
    """A List of Hashtags on the Video"""
    as_dict: dict
    """The raw data associated with this Video."""

    def __init__(
            self,
            id: Optional[str] = None,
            username: Optional[str] = None,
            url: Optional[str] = None,
            data: Optional[dict] = None,
            parent: Optional["PyTok"] = None,
    ):
        """
        You must provide the id or a valid url, else this will fail.
        """
        self.parent = parent
        self.id = id
        self.username = username
        if data is not None:
            self.as_dict = data
            self.__extract_from_data()
        elif url is not None:
            self.id = extract_video_id_from_url(url)
            self.username = extract_user_id_from_url(url)

        if self.id is None and url is None:
            raise TypeError("You must provide id or url parameter.")

    async def info(self, **kwargs) -> dict:
        """
        Returns a dictionary of all data associated with a TikTok Video.

        Example Usage
        ```py
        video_data = api.video(id='7041997751718137094').info()
        ```
        """
        if not hasattr(self, 'as_dict'):
            try:
                video_data = await self._info_api(**kwargs)
            except Exception as ex:
                self.parent.logger.debug(f"API info fetch failed with exception: {ex}, falling back to scraping")
                video_data = await self._info_scraping(**kwargs)
                
            self.as_dict = video_data
        else:
            video_data = self.as_dict

        return video_data
    
    async def _info_api(self, **kwargs) -> dict:
        url = self._get_url()
        _, session = self.parent.tiktok_api._get_session()
        r = requests.get(url, headers=session.headers)
        if r.status_code != 200:
            raise exceptions.NotAvailableException(
                f"TikTok returned status {r.status_code}"
            )
        contents = extract_tag_contents(r.text)
        if not contents:
            raise exceptions.NotAvailableException(
                "Could not extract data from response"
            )
        data = json.loads(contents)
        # Extract video data from the parsed JSON structure
        default_scope = data.get("__DEFAULT_SCOPE__", {})
        video_detail = default_scope.get("webapp.video-detail", {})
        video_info = video_detail.get("itemInfo", {}).get("itemStruct")
        if video_info is None:
            # Try SIGI_STATE format
            if self.id in data.get("ItemModule", {}):
                video_info = data["ItemModule"][self.id]
            else:
                raise exceptions.NotAvailableException(
                    "Could not extract video info from response"
                )
        return video_info
    
    async def _info_scraping(self, **kwargs) -> dict:
        url = self._get_url()
        page = self.parent._page
        if page.url != url:
            await self.view()

        # Get video data from page HTML
        html_body = await page.get_content()
        contents = extract_tag_contents(html_body)

        if not contents:
            raise exceptions.InvalidJSONException("Could not find data script tag in page")

        res = json.loads(contents)

        video_detail = res.get('__DEFAULT_SCOPE__', {}).get('webapp.video-detail', {})
        if not video_detail:
            raise exceptions.InvalidJSONException("Could not find video detail in page data")

        if video_detail.get('statusCode') != 0:
            raise exceptions.NotAvailableException(
                f"Content is not available with status message: {video_detail.get('statusMsg', 'Unknown error')}")

        video_data = video_detail.get('itemInfo', {}).get('itemStruct', {})
        if not video_data:
            raise exceptions.InvalidJSONException("Could not find video data in page")

        return video_data

    async def network_info(self, **kwargs) -> dict:
        """
        Returns a dictionary of all network data associated with a TikTok Video.

        Example Usage
        ```py
        video_data = api.video(id='7041997751718137094').network_data()
        ```
        """
        url = self._get_url()
        page = self.parent._page
        if page.url != url:
            await self.view()

        responses = self.get_responses(url)
        if not responses:
            raise Exception("No responses found for video page")

        resp = responses[-1]
        cdp_response = resp.get('response')

        network_info = {}
        if cdp_response:
            network_info['server_addr'] = getattr(cdp_response, 'remote_ip_address', None)
            network_info['headers'] = getattr(cdp_response, 'headers', {})
        return network_info

    async def bytes_network_info(self, **kwargs) -> dict:
        """
        Returns a dictionary of all network data associated with a TikTok Video.

        Example Usage
        ```py
        video_data = api.video(id='7041997751718137094').bytes_network_data()
        ```
        """
        play_path = url_parsers.urlparse(self.as_dict['video']['playAddr']).path
        responses = self.get_responses(play_path)
        if not responses:
            raise Exception("No responses found for video bytes")

        for resp in responses:
            cdp_response = resp.get('response')
            if cdp_response:
                network_info = {}
                network_info['server_addr'] = getattr(cdp_response, 'remote_ip_address', None)
                network_info['headers'] = getattr(cdp_response, 'headers', {})
                return network_info

        raise Exception("Failed to get video bytes network info")

    def _get_url(self) -> str:
        if self.username is not None:
            return f"https://www.tiktok.com/@{self.username}/video/{self.id}"
        else:
            # will autoresolve to correct username
            return f"https://www.tiktok.com/@user/video/{self.id}"

    async def view(self, **kwargs) -> None:
        """
        Opens the TikTok Video in your default browser.

        Example Usage
        ```py
        api.video(id='7041997751718137094').view()
        ```
        """
        page = self.parent._page
        url = self._get_url()
        if page.url == url:
            return

        self.parent.logger.debug(f"Loading video page: {url}")
        await page.get(url)
        await asyncio.sleep(5)  # Wait for page to fully load

        # Check for unavailable content
        await self.wait_for_content_or_unavailable('[id="main-content-video_detail"]', 'Video currently unavailable')
        
    async def _related_videos(self, counter, count=20):
        data_request_path = "api/related/item_list"
        # Process pending responses via CDP
        responses = await self.parent.process_pending_responses(data_request_path)

        for resp in responses:
            # parse params from url
            url_parsed = url_parsers.urlparse(resp.get('url', ''))
            params = url_parsers.parse_qs(url_parsed.query)
            if params.get('itemID', [''])[0] != self.id:
                continue

            body = resp.get('body', '')
            if not body:
                continue

            try:
                d = json.loads(body) if isinstance(body, str) else body
                for v in d.get('itemList', []):
                    yield v
                    counter.add(1)
                if counter.get() >= count:
                    break
            except Exception as e:
                self.parent.logger.debug(f"Error parsing related videos response: {e}")

    async def related_videos(self, count=20) -> list[dict]:
        """
        Returns a list of related TikTok Videos to the current Video.

        Uses API-first approach with fallback to scraping.
        """
        try:
            async for video in self._related_videos_api(count=count):
                yield video
        except exceptions.ApiFailedException as ex:
            self.parent.logger.warning(f"API related videos fetch failed: {ex}. Falling back to scraping method.")
            async for video in self._related_videos_scraping(count=count):
                yield video

    async def _related_videos_api(self, count=20) -> list[dict]:
        """Get related videos using TikTok API directly via make_request."""
        self.parent.logger.debug(f"Starting _related_videos_api for video {self.id}")
        amount_yielded = 0

        params = {
            'itemID': self.id,
            'count': 16,
        }

        while count is None or amount_yielded < count:
            self.parent.logger.debug(f"Making TikTok-Api request for related videos")
            try:
                res = await self.parent.tiktok_api.make_request(
                    url="https://www.tiktok.com/api/related/item_list/",
                    params=params,
                )
            except Exception as e:
                self.parent.logger.warning(f"make_request failed for related videos: {e}")
                raise exceptions.ApiFailedException(f"TikTok-Api make_request failed: {e}")

            if res is None:
                raise exceptions.ApiFailedException("TikTok-Api returned None response")

            if res.get('type') == 'verify':
                raise exceptions.ApiFailedException("TikTok API is asking for verification")

            status_code = res.get('statusCode', 0)
            if status_code != 0:
                status_msg = res.get('statusMsg', 'Unknown error')
                raise exceptions.ApiFailedException(
                    f"TikTok returned error for related videos: statusCode={status_code}, statusMsg={status_msg}"
                )

            videos = res.get('itemList', [])
            self.parent.logger.debug(f"Got {len(videos)} related videos from API")

            if videos:
                for video in videos:
                    yield video
                    amount_yielded += 1
                    if count is not None and amount_yielded >= count:
                        return

            # Related videos API doesn't have pagination cursor, just return what we got
            return

    async def _related_videos_scraping(self, count=20) -> list[dict]:
        page = self.parent._page
        url = self._get_url()

        # Ensure we're on the video page
        if page.url != url:
            await self.view()

        counter = Counter()
        async for video in self._related_videos(counter, count=count):
            yield video

        # get via scroll / solve captcha if necessary
        if counter.get() == 0:
            await self.check_and_wait_for_captcha()
            # Reload page using nodriver pattern
            await page.get(url)
            await asyncio.sleep(5)
            await self.parent.process_pending_responses()
            async for video in self._related_videos(counter, count=count):
                yield video

    async def bytes(self, timeout=60) -> bytes:
        """
        Returns the bytes of a TikTok Video.

        Example Usage
        ```py
        video_bytes = api.video(id='7041997751718137094').bytes()

        # Saving The Video
        with open('saved_video.mp4', 'wb') as output:
            output.write(video_bytes)
        ```
        """
        # playAddr is the highest-bitrate variant; prefer it for quality, fall back to download.
        bytes_urls = [
            self.as_dict['video'].get('playAddr'),
            self.as_dict['video'].get('downloadAddr'),
        ]
        bytes_urls = [url for url in bytes_urls if url]
        if not bytes_urls:
            raise exceptions.NotAvailableException("Post does not have a video")

        # If the bytes were already captured on the wire (e.g. during playback), use them.
        paths = [url_parsers.urlparse(url).path for url in bytes_urls]
        cached = self._find_cached_bytes(paths)
        if cached is not None and self._looks_like_video(cached):
            return cached

        # Otherwise fetch the signed CDN URL ourselves. The CDN authorizes by session cookie,
        # so the request must carry credentials — a cookieless fetch 403s. See pytok memory:
        # video-cdn-cors-block.
        #
        # Prefer a direct httpx download carrying the browser's session cookies: it's much
        # faster for large files than the in-browser fetch, which has to base64-encode the
        # whole video and ship it back over the CDP websocket. Fall back to the in-browser
        # fetch() only if the direct request is rejected (e.g. the CDN ever tightens checks).
        req_exceptions = []
        for fetcher_name, fetcher in (("httpx", self._httpx_fetch_bytes),
                                      ("browser", self._browser_fetch_bytes)):
            for bytes_url in bytes_urls:
                try:
                    data = await asyncio.wait_for(fetcher(bytes_url), timeout=timeout)
                except Exception as ex:
                    req_exceptions.append(f"{fetcher_name}: {ex}")
                    continue
                if self._looks_like_video(data):
                    return data
                req_exceptions.append(f"{fetcher_name}: {bytes_url[:60]}... did not return an MP4")
        raise Exception(f"Failed to get video bytes, exceptions: {req_exceptions}")

    def _find_cached_bytes(self, paths) -> Optional[bytes]:
        """Return video bytes from the CDP response cache if present, else None."""
        for path in paths:
            for res in self.get_responses(path):
                if res.get('body'):
                    return res['body']
        return None

    @staticmethod
    def _looks_like_video(body) -> bool:
        """True if body is a complete progressive MP4 (starts with an ISO 'ftyp' box).
        Other large CDN blobs (DASH fragments, separate audio, range probes) lack this."""
        return isinstance(body, (bytes, bytearray)) and body[4:8] == b'ftyp'

    async def _httpx_fetch_bytes(self, bytes_url) -> bytes:
        """Download the signed CDN URL directly with httpx, carrying the browser's session
        cookies (the CDN authorizes by cookie) and the browser session's headers. Avoids the
        base64-over-CDP round trip of the in-browser fetch, so it's much faster for large files
        and can run concurrently with browser activity."""
        from zendriver import cdp
        cookie_result = await self.parent._page.send(cdp.network.get_cookies([bytes_url]))
        cookies = {cookie.name: cookie.value for cookie in cookie_result}
        _, session = self.parent.tiktok_api._get_session()
        headers = dict(session.headers)
        headers['Referer'] = 'https://www.tiktok.com/'
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            resp = await client.get(bytes_url, headers=headers, cookies=cookies)
        if resp.status_code not in (200, 206):
            raise Exception(f"httpx fetch returned status {resp.status_code}")
        return resp.content

    async def _browser_fetch_bytes(self, bytes_url) -> bytes:
        """Fetch the video via an in-page fetch() so the request goes through Chrome's network
        stack with the session cookies (credentials:'include'), then ship the bytes back
        base64-encoded. The CDN authorizes by cookie, so credentials are required."""
        js = (
            "(async () => { try {"
            f"  const resp = await fetch({json.dumps(bytes_url)}, {{ method: 'GET', credentials: 'include' }});"
            "  if (!resp.ok) return JSON.stringify({ok: false, status: resp.status});"
            "  const bytes = new Uint8Array(await resp.arrayBuffer());"
            "  let binary = ''; const chunk = 0x8000;"
            "  for (let i = 0; i < bytes.length; i += chunk) {"
            "    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk)); }"
            "  return JSON.stringify({ok: true, b64: btoa(binary)});"
            "} catch (e) { return JSON.stringify({ok: false, error: String(e)}); } })()"
        )
        result = await self.parent.tiktok_api._evaluate(self.parent._page, js, await_promise=True)
        if not result:
            raise Exception("In-browser byte fetch returned no result")
        data = json.loads(result)
        if not data.get("ok"):
            raise Exception(f"In-browser byte fetch failed: status={data.get('status')} {data.get('error', '')}")
        return base64.b64decode(data["b64"])

    async def _get_comments_and_req(self, count):
        # get request
        data_request_path = "api/comment/list"
        # Process pending responses via CDP
        data_responses = await self.parent.process_pending_responses(data_request_path)

        amount_yielded = 0
        all_comments = []
        processed_urls = []

        for data_response in data_responses:
            try:
                url = data_response.get('url', '')
                body = data_response.get('body', '')

                if not body:
                    continue

                res = json.loads(body) if isinstance(body, str) else body

                # Store the URL and response info for later use
                self.parent.request_cache['comments'] = {
                    'url': url,
                    'response': data_response.get('response')
                }

                processed_urls.append(url)

                comments = res.get("comments", [])

                amount_yielded += len(comments)
                all_comments += comments

                if amount_yielded > count:
                    return all_comments, processed_urls, True

                has_more = res.get("has_more")
                if has_more != 1:
                    self.parent.logger.info(
                        "TikTok isn't sending more TikToks beyond this point."
                    )
                    return all_comments, processed_urls, True
            except Exception:
                pass

        return all_comments, processed_urls, False

    async def _get_comment_replies(self, comment, batch_size):
        if 'comments' not in self.parent.request_cache:
            return
        data_request = self.parent.request_cache['comments']
        num_already_fetched = len(
            comment.get('reply_comment', []) if comment.get('reply_comment', []) is not None else [])
        num_comments_to_fetch = comment['reply_comment_total'] - num_already_fetched

        # Get the URL from the cached data
        cached_url = data_request.get('url', '') if isinstance(data_request, dict) else getattr(data_request, 'url', '')

        while num_comments_to_fetch > 0:
            url_parsed = url_parsers.urlparse(cached_url)
            params = url_parsers.parse_qs(url_parsed.query)
            params['cursor'] = num_already_fetched
            if 'aweme_id' in params:
                del params['aweme_id']
            params['count'] = min(num_comments_to_fetch, batch_size)
            params['item_id'] = comment['aweme_id']
            params['comment_id'] = comment['cid']
            params['focus_state'] = 'true'
            url_path = url_parsed.path.replace("api/comment/list", "api/comment/list/reply")
            next_url = f"{url_parsed.scheme}://{url_parsed.netloc}{url_path}?{url_parsers.urlencode(params, doseq=True)}"

            # Get cookies via CDP
            from zendriver import cdp
            cookie_result = await self.parent._page.send(cdp.network.get_cookies())
            cookies = {cookie.name: cookie.value for cookie in cookie_result}

            # Get headers from TikTok-Api session
            _, session = self.parent.tiktok_api._get_session()
            headers = dict(session.headers)

            r = requests.get(next_url, headers=headers, cookies=cookies)

            if not r.content:
                return

            res = r.json()

            reply_comments = res.get("comments", [])

            if reply_comments:
                comment['reply_comment'] = comment['reply_comment'] + reply_comments if comment[
                    'reply_comment'] else reply_comments

            has_more = res.get("has_more")
            if has_more != 1:
                self.parent.logger.info(
                    "TikTok isn't sending more TikToks beyond this point."
                )
                break

            await self.parent.request_delay()

            num_already_fetched = len(comment['reply_comment'])
            num_comments_to_fetch = comment['reply_comment_total'] - num_already_fetched

    async def comments(self, count=200, batch_size=100, fetch_replies=True):
        """
        Returns comments for this video.

        Uses API-first approach with fallback to scraping.
        """
        try:
            async for comment in self._comments_api(count=count, batch_size=batch_size, fetch_replies=fetch_replies):
                yield comment
        except exceptions.ApiFailedException as ex:
            self.parent.logger.warning(f"API comments fetch failed: {ex}. Falling back to scraping method.")
            async for comment in self._comments_scraping(count=count, batch_size=batch_size, fetch_replies=fetch_replies):
                yield comment

    async def _comments_api(self, count=200, batch_size=100, fetch_replies=True):
        """Get comments using TikTok API directly via make_request."""
        self.parent.logger.debug(f"Starting _comments_api for video {self.id}")
        amount_yielded = 0
        cursor = 0

        while count is None or amount_yielded < count:
            params = {
                'aweme_id': self.id,
                'count': 20,
                'cursor': cursor,
                'from_page': 'video',
                'root_referer': 'https://www.tiktok.com/',
            }

            self.parent.logger.debug(f"Making TikTok-Api request for comments with cursor={cursor}")
            try:
                res = await self.parent.tiktok_api.make_request(
                    url="https://www.tiktok.com/api/comment/list/",
                    params=params,
                )
            except Exception as e:
                self.parent.logger.warning(f"make_request failed for comments: {e}")
                raise exceptions.ApiFailedException(f"TikTok-Api make_request failed: {e}")

            if res is None:
                raise exceptions.ApiFailedException("TikTok-Api returned None response")

            if res.get('type') == 'verify':
                raise exceptions.ApiFailedException("TikTok API is asking for verification")

            status_code = res.get('status_code', 0)
            if status_code != 0:
                status_msg = res.get('status_msg', 'Unknown error')
                raise exceptions.ApiFailedException(
                    f"TikTok returned error for comments: status_code={status_code}, status_msg={status_msg}"
                )

            comments = res.get('comments', [])

            if comments is None:
                raise exceptions.NoContentException("No comments found for this video")

            self.parent.logger.debug(f"Got {len(comments)} comments from API")

            if comments:
                for comment in comments:
                    # Get comment replies if available
                    if fetch_replies and comment.get('reply_comment_total', 0) > 0:
                        try:
                            await self._get_comment_replies_api(comment, batch_size)
                        except Exception:
                            pass
                    yield comment
                    amount_yielded += 1
                    if count is not None and amount_yielded >= count:
                        return

            has_more = res.get('has_more')
            if has_more != 1:
                self.parent.logger.info("TikTok isn't sending more comments beyond this point.")
                return

            cursor = res.get('cursor', cursor)
            await self.parent.request_delay()

    async def _get_comment_replies_api(self, comment, batch_size):
        """Get comment replies using TikTok API directly via make_request."""
        num_already_fetched = len(comment.get('reply_comment', []) or [])
        num_comments_to_fetch = comment.get('reply_comment_total', 0) - num_already_fetched
        cursor = num_already_fetched

        while num_comments_to_fetch > 0:
            params = {
                'item_id': comment.get('aweme_id', self.id),
                'comment_id': comment['cid'],
                'count': min(num_comments_to_fetch, batch_size),
                'cursor': cursor,
                'from_page': 'video',
                'root_referer': 'https://www.tiktok.com/',
            }

            try:
                res = await self.parent.tiktok_api.make_request(
                    url="https://www.tiktok.com/api/comment/list/reply/",
                    params=params,
                )
            except Exception as e:
                self.parent.logger.debug(f"Failed to get comment replies via API: {e}")
                return

            if res is None:
                return

            reply_comments = res.get('comments', [])

            if reply_comments:
                if comment.get('reply_comment'):
                    comment['reply_comment'] = comment['reply_comment'] + reply_comments
                else:
                    comment['reply_comment'] = reply_comments

            has_more = res.get('has_more')
            if has_more != 1:
                break

            await self.parent.request_delay()

            num_already_fetched = len(comment.get('reply_comment', []) or [])
            num_comments_to_fetch = comment.get('reply_comment_total', 0) - num_already_fetched
            cursor = num_already_fetched

    async def _comments_scraping(self, count=200, batch_size=100, fetch_replies=True):
        """Get comments by scraping the video page."""
        if (self.id and self.username) or self.as_dict:
            await self.view()

            # Comments may not auto-load; click the comment icon to open the panel
            page = self.parent._page
            try:
                comment_icon = await page.select('[data-e2e="comment-icon"]', timeout=3)
                if comment_icon:
                    await comment_icon.mouse_click()
                    await asyncio.sleep(2)
            except Exception:
                pass

            await self.wait_for_content_or_unavailable_or_captcha('css=[data-e2e=comment-level-1]',
                                                                  'Be the first to comment!')

            amount_yielded = 0
            all_comments, processed_urls, finished = await self._get_comments_and_req(count)

            if fetch_replies:
                for comment in all_comments:
                    await self._get_comment_replies(comment, batch_size)

            amount_yielded += len(all_comments)
            for comment in all_comments:
                yield comment

            if finished:
                return

            # so that we don't re-yield any comments previously yielded
            comment_ids = set(comment['cid'] for comment in all_comments)
            try:
                async for comment in self._get_api_comments(count, batch_size, comment_ids, fetch_replies=fetch_replies):
                    yield comment
            except exceptions.ApiFailedException:
                async for comment in self._get_scroll_comments(count, amount_yielded, processed_urls, fetch_replies=fetch_replies):
                    yield comment
        else:
            # if we only have the video id, fall back to scroll-based scraping
            raise exceptions.ApiFailedException("Cannot scrape comments without username - need page navigation")

    async def _get_scroll_comments(self, count, amount_yielded, processed_urls, fetch_replies=True):
        page = self.parent._page
        if page.url != self._get_url():
            await self.view()
        tries = 0

        data_request_path = "api/comment/list"
        while amount_yielded < count:
            # scroll down to induce request
            await self.scroll_to(10000)
            await self.slight_scroll_up()
            await self.check_and_wait_for_captcha()
            await self.check_and_close_signin()

            # Process pending responses via CDP
            data_responses = await self.parent.process_pending_responses(data_request_path)
            data_responses = [resp for resp in data_responses if resp.get('url', '') not in processed_urls]

            if len(data_responses) == 0:
                if tries > 5:
                    logger.debug(f"Not sending anymore!")
                    break
                tries += 1

            for data_response in data_responses:
                try:
                    url = data_response.get('url', '')
                    body = data_response.get('body', '')

                    if not body:
                        continue

                    res = json.loads(body) if isinstance(body, str) else body

                    processed_urls.append(url)

                    comments = res.get("comments", [])

                    if fetch_replies:
                        for comment in comments:
                            await self._get_comment_replies(comment, 100)

                    amount_yielded += len(comments)
                    for comment in comments:
                        yield comment

                    if amount_yielded > count:
                        return

                    has_more = res.get("has_more")
                    if has_more != 1:
                        self.parent.logger.info(
                            "TikTok isn't sending more TikToks beyond this point."
                        )
                        return
                except Exception as e:
                    processed_urls.append(data_response.get('url', ''))

    async def _get_comments_via_requests(self, count, cursor, data_request):
        # Get the URL from the cached data (can be dict or object)
        cached_url = data_request.get('url', '') if isinstance(data_request, dict) else getattr(data_request, 'url', '')
        next_url = edit_url(cached_url, {'count': count, 'cursor': cursor, 'aweme_id': self.id})

        # Get cookies via CDP
        from zendriver import cdp
        cookie_result = await self.parent._page.send(cdp.network.get_cookies())
        cookies = {cookie.name: cookie.value for cookie in cookie_result}

        # Get headers from TikTok-Api session
        _, session = self.parent.tiktok_api._get_session()
        headers = dict(session.headers)
        headers['referer'] = 'https://www.tiktok.com/'

        r = requests.get(next_url, headers=headers, cookies=cookies)

        if r.status_code != 200:
            raise Exception(f"Failed to get comments with status code {r.status_code}")

        if len(r.content) == 0:
            logger.debug("Failed to get comments from API, switching to scroll")
            raise exceptions.ApiFailedException("No content in response")

        try:
            res = r.json()
        except Exception:
            res = json.loads(brotli.decompress(r.content).decode())

        return res

    async def _get_api_comments(self, count, batch_size, comment_ids, fetch_replies=True):
        data_request = self.parent.request_cache['comments']

        amount_yielded = len(comment_ids)
        cursor = 0

        while amount_yielded < count:
            try:
                res = await self._get_comments_via_requests(20, cursor, data_request)

                if res.get('type') == 'verify':
                    # force new request for cache
                    await self._get_comments_and_req(count)
                    continue

                cursor = res.get("cursor", 0)
                comments = res.get("comments", [])

                if comments:
                    for comment in comments:
                        if comment['cid'] not in comment_ids:
                            if fetch_replies:
                                try:
                                    await self._get_comment_replies(comment, batch_size)
                                except Exception:
                                    pass
                            yield comment
                            amount_yielded += 1

                has_more = res.get("has_more")
                if has_more != 1:
                    self.parent.logger.info(
                        "TikTok isn't sending more TikToks beyond this point."
                    )
                    return

                await self.parent.request_delay()

            except Exception as e:
                self.parent.logger.debug(f"Error getting comments via API: {e}")
                raise exceptions.ApiFailedException(f"Failed to get comments: {e}")

    def __extract_from_data(self) -> None:
        data = self.as_dict
        keys = data.keys()

        if "author" in keys:
            self.id = data["id"]
            self.username = data["author"]["uniqueId"]
            self.create_time = datetime.fromtimestamp(int(data["createTime"]))
            self.stats = data["stats"]
            self.author = self.parent.user(data=data["author"])
            self.sound = self.parent.sound(data=data["music"])

            self.hashtags = [
                self.parent.hashtag(data=hashtag)
                for hashtag in data.get("challenges", [])
            ]

        if self.id is None:
            self.parent.logger.error(
                f"Failed to create Video with data: {data}\nwhich has keys {data.keys()}"
            )

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return f"PyTok.video(id='{self.id}')"

    # def __getattr__(self, name):
    #     # Handle author, sound, hashtags, as_dict
    #     if name in ["author", "sound", "hashtags", "stats", "create_time", "as_dict"]:
    #         self.as_dict = self.info()
    #         self.__extract_from_data()
    #         return self.__getattribute__(name)

    #     raise AttributeError(f"{name} doesn't exist on PyTok.api.Video")
