from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional
from urllib import parse as url_parsers

import brotli
import httpx
import requests

if TYPE_CHECKING:
    from ..tiktok import PyTok
    from .hashtag import Hashtag
    from .sound import Sound
    from .user import User

from .. import exceptions
from ..helpers import (
    edit_url,
    extract_tag_contents,
    extract_user_id_from_url,
    extract_video_id_from_url,
)
from .base import Base

logger = logging.getLogger("pytok.api.video")


class _CdnRejected(Exception):
    """The CDN answered a direct media download with an error status."""


def _image_extension(body) -> Optional[str]:
    """File extension for an image body, from its magic bytes; None if it isn't an image."""
    if not isinstance(body, (bytes, bytearray)):
        return None
    if body[:3] == b'\xff\xd8\xff':
        return "jpg"
    if body[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    if body[:4] == b'RIFF' and body[8:12] == b'WEBP':
        return "webp"
    if body[:4] == b'GIF8':
        return "gif"
    if body[4:8] == b'ftyp' and body[8:12] in (b'heic', b'heix', b'mif1', b'msf1'):
        return "heic"
    if body[4:8] == b'ftyp' and body[8:12] in (b'avif', b'avis'):
        return "avif"
    return None


def _audio_extension(body) -> Optional[str]:
    """File extension for an audio body, from its magic bytes; None if it isn't audio."""
    if not isinstance(body, (bytes, bytearray)) or len(body) < 12:
        return None
    if body[:3] == b'ID3':
        return "mp3"
    if body[0] == 0xff and body[1] & 0xe0 == 0xe0:
        # an MPEG frame sync; layer bits 00 mean ADTS AAC rather than MP3
        return "aac" if body[1] & 0x06 == 0 else "mp3"
    if body[4:8] == b'ftyp':
        return "m4a"
    if body[:4] == b'OggS':
        return "ogg"
    if body[:4] == b'RIFF' and body[8:12] == b'WAVE':
        return "wav"
    if body[:4] == b'fLaC':
        return "flac"
    return None


@dataclass
class PostMedia:
    """The media of a post: the MP4 of a video post, or the images (in display order) and
    sound of a photo/slideshow post."""

    id: str
    video: Optional[bytes] = None
    images: list[bytes] = field(default_factory=list)
    audio: Optional[bytes] = None
    """A photo post's sound; None for a video post (its MP4 carries the audio) or a photo
    post without a downloadable sound."""

    @property
    def is_photo_post(self) -> bool:
        return self.video is None

    def save(self, directory) -> list[Path]:
        """Write the media into `directory`: ``<id>.mp4`` for a video post, or ``<id>_01.jpg``,
        ``<id>_02.jpg``, ... and ``<id>.m4a`` for a photo post, with extensions taken from the
        content. Returns the paths written."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        files = []
        if self.video is not None:
            files.append((f"{self.id}.mp4", self.video))
        width = max(2, len(str(len(self.images))))
        for n, image in enumerate(self.images, start=1):
            files.append((f"{self.id}_{n:0{width}d}.{_image_extension(image) or 'bin'}", image))
        if self.audio is not None:
            files.append((f"{self.id}.{_audio_extension(self.audio) or 'bin'}", self.audio))
        paths = []
        for name, body in files:
            paths.append(directory / name)
            paths[-1].write_bytes(body)
        return paths


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
        html_body = await page.content()
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
        return {'server_addr': resp.get('server_addr'), 'headers': resp.get('headers', {})}

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

        resp = responses[0]
        return {'server_addr': resp.get('server_addr'), 'headers': resp.get('headers', {})}

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
        await self.parent.navigate(url, wait_until="domcontentloaded")
        await asyncio.sleep(5)  # Wait for page to fully load

        # Check for unavailable content
        await self.wait_for_content_or_unavailable('[id="main-content-video_detail"]', 'Video currently unavailable')

    async def _related_videos(self, counter, count=20):
        data_request_path = "api/related/item_list"
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
            self.parent.logger.debug("Making API request for related videos")
            try:
                res = await self.parent.tiktok_api.make_request(
                    url="https://www.tiktok.com/api/related/item_list/",
                    params=params,
                )
            except Exception as e:
                self.parent.logger.warning(f"make_request failed for related videos: {e}")
                raise exceptions.ApiFailedException(f"API request failed: {e}")

            if res is None:
                raise exceptions.ApiFailedException("API returned None response")

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
            await self.parent.navigate(url, wait_until="domcontentloaded")
            await asyncio.sleep(5)
            await self.parent.process_pending_responses()
            async for video in self._related_videos(counter, count=count):
                yield video

    @property
    def is_photo_post(self) -> bool:
        """True for a photo/slideshow post, whose media is images rather than a video."""
        return bool(getattr(self, 'as_dict', None)) and 'imagePost' in self.as_dict

    async def media(self, timeout=60) -> PostMedia:
        """
        Downloads the media of a TikTok post: the MP4 of a video post, or the images and sound
        of a photo/slideshow post.

        Example Usage
        ```py
        media = await api.video(id='7041997751718137094').media()
        media.save('media/')  # <id>.mp4, or <id>_01.jpg, <id>_02.jpg, ..., <id>.m4a

        if not media.is_photo_post:
            mp4 = media.video
        ```

        `timeout` bounds every wait on the browser and how long a direct download may go
        without receiving data, not the whole download: a large video still arriving on a
        slow link is not cut off.

        Raises NoVideoException for a post with neither a video nor images to download.
        """
        if self.is_photo_post:
            return await self._photo_post_media(timeout)

        # playAddr is the highest-bitrate variant; prefer it for quality, fall back to
        # download. Carry the variant name so a failure can say which URL was tried.
        video_urls = [
            ("playAddr", self.as_dict['video'].get('playAddr')),
            ("downloadAddr", self.as_dict['video'].get('downloadAddr')),
        ]
        video_urls = [(name, url) for name, url in video_urls if url]
        if not video_urls:
            raise exceptions.NoVideoException(f"Post {self.id} has no video or images to download")

        video = await self._fetch_media(
            video_urls, timeout, self._looks_like_video, "an MP4", f"the video of post {self.id}")
        return PostMedia(id=self.id, video=video)

    async def _photo_post_media(self, timeout) -> PostMedia:
        images = []
        for n, image in enumerate(self.as_dict['imagePost'].get('images') or [], start=1):
            # urlList holds the same image on different CDN hosts
            urls = (image.get('imageURL') or {}).get('urlList') or []
            named_urls = [(f"image{n}/url{i}", url) for i, url in enumerate(urls) if url]
            images.append(await self._fetch_media(
                named_urls, timeout, lambda body: _image_extension(body) is not None,
                "an image", f"image {n} of photo post {self.id}"))
        if not images:
            raise exceptions.NoVideoException(f"Photo post {self.id} has no images to download")

        # a muted "original sound" carries no playUrl
        audio = None
        audio_url = (self.as_dict.get('music') or {}).get('playUrl')
        if audio_url:
            audio = await self._fetch_media(
                [("music/playUrl", audio_url)], timeout,
                lambda body: _audio_extension(body) is not None,
                "audio", f"the sound of photo post {self.id}")
        return PostMedia(id=self.id, images=images, audio=audio)

    async def _fetch_media(self, named_urls, timeout, is_valid: Callable[[bytes], bool],
                           kind: str, what: str) -> bytes:
        """Download the first of `named_urls` ((name, url) pairs for the same file) whose body
        passes `is_valid`. `kind` names the expected content and `what` the file, for errors."""
        # If the bytes were already captured on the wire (e.g. during playback), use them.
        paths = [url_parsers.urlparse(url).path for _, url in named_urls]
        cached = self._find_cached_bytes(paths)
        if cached is not None and is_valid(cached):
            return cached

        # Otherwise fetch the signed CDN URL ourselves, directly with httpx: the in-browser
        # fetch has to base64-encode the whole file and ship it back through the browser
        # driver, so it is only worth trying when the CDN rejected the direct request (e.g.
        # it ever tightens checks). A direct download that timed out or dropped would fare
        # no better going through the browser's network stack.
        req_exceptions = []
        rejected = False
        for url_name, bytes_url in named_urls:
            label = f"httpx/{url_name}"
            started = time.monotonic()
            try:
                data = await self._httpx_fetch_bytes(bytes_url, timeout)
            except asyncio.TimeoutError:
                # only the cookie read waits on the browser, and a page that does not answer
                # that will not answer the in-page fallback either
                req_exceptions.append(f"{label}: reading the browser's cookies timed out after {timeout}s")
                break
            except _CdnRejected as ex:
                rejected = True
                req_exceptions.append(f"{label}: {ex}")
            except Exception as ex:
                req_exceptions.append(
                    f"{label}: {self._describe_exception(ex)} (after {time.monotonic() - started:.1f}s)"
                )
            else:
                if is_valid(data):
                    return data
                rejected = True
                req_exceptions.append(f"{label}: {self._describe_body(data, kind)}")

        if rejected:
            for url_name, bytes_url in named_urls:
                label = f"browser/{url_name}"
                started = time.monotonic()
                try:
                    data = await asyncio.wait_for(self._browser_fetch_bytes(bytes_url), timeout=timeout)
                except asyncio.TimeoutError:
                    # asyncio.TimeoutError stringifies to '', so name it outright
                    req_exceptions.append(f"{label}: timed out after {timeout}s")
                except Exception as ex:
                    req_exceptions.append(
                        f"{label}: {self._describe_exception(ex)} (after {time.monotonic() - started:.1f}s)"
                    )
                else:
                    if is_valid(data):
                        return data
                    req_exceptions.append(f"{label}: {self._describe_body(data, kind)}")
        raise Exception(
            f"Failed to get {what}: " + "; ".join(req_exceptions)
        )

    @staticmethod
    def _describe_exception(ex) -> str:
        """Never interpolate a bare exception: plenty of them stringify to ''."""
        if not str(ex):
            return type(ex).__name__
        if type(ex) is Exception:
            return str(ex)  # raised here with a written-out message already
        return f"{type(ex).__name__}: {ex}"

    @staticmethod
    def _describe_body(body, kind="an MP4") -> str:
        """Say what a response that wasn't `kind` actually was, so a CDN error page, an empty
        body and a DASH fragment are told apart."""
        if body is None:
            return "returned no data"
        if not isinstance(body, (bytes, bytearray)):
            return f"returned {type(body).__name__}, not bytes"
        if not body:
            return "returned 0 bytes"
        head = bytes(body[:12])
        if head.lstrip()[:1] in (b'<', b'{'):
            # an HTML/JSON error page served with a 200
            return f"returned {len(body)} bytes of markup/JSON, not {kind} ({head!r})"
        if kind == "an MP4":
            return f"returned {len(body)} bytes but no ftyp box, not a progressive MP4 ({head!r})"
        return f"returned {len(body)} bytes that are not {kind} ({head!r})"

    def _find_cached_bytes(self, paths) -> Optional[bytes]:
        """Return a file's bytes from the captured response cache if present, else None."""
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

    # Resumes allowed after the CDN closes a download partway. Each must make progress, so
    # this only bounds a connection that keeps dropping.
    MAX_DOWNLOAD_RESUMES = 20

    async def _httpx_fetch_bytes(self, bytes_url, timeout) -> bytes:
        """Download the signed CDN URL directly with httpx, over the session's shared media
        client (which carries the browser's cookies) and with the browser session's headers.

        `timeout` bounds the cookie read, raising asyncio.TimeoutError, and each wait on the
        network, raising httpx.TimeoutException; it does not bound the whole transfer, so a
        large video still arriving on a slow link is not cut off and restarted. The CDN closes
        long transfers partway, so a cut download resumes with a Range request from where it
        stopped rather than starting over.
        """
        client = await self.parent._media_client(timeout)
        _, session = self.parent.tiktok_api._get_session()
        headers = dict(session.headers)
        headers['Referer'] = 'https://www.tiktok.com/'
        # byte offsets only line up across a resume when the body isn't content-encoded
        headers['Accept-Encoding'] = 'identity'

        body = bytearray()
        resumes = 0
        while True:
            request_headers = dict(headers)
            if body:
                request_headers['Range'] = f"bytes={len(body)}-"
            received = 0
            encoded = False
            try:
                async with client.stream("GET", bytes_url, headers=request_headers,
                                         timeout=httpx.Timeout(timeout)) as resp:
                    if resp.status_code in (401, 403):
                        self.parent._expire_media_cookies()
                    if resp.status_code not in (200, 206):
                        raise _CdnRejected(f"httpx fetch returned status {resp.status_code}")
                    if body and self._range_start(resp) != len(body):
                        raise Exception(
                            f"resuming at byte {len(body)} got {resp.status_code} "
                            f"{resp.headers.get('content-range', 'without Content-Range')}"
                        )
                    encoded = resp.headers.get('content-encoding', 'identity') != 'identity'
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        received += len(chunk)
                return bytes(body)
            except (httpx.RemoteProtocolError, httpx.ReadError):
                if not received or encoded or resumes >= self.MAX_DOWNLOAD_RESUMES:
                    raise
                resumes += 1

    @staticmethod
    def _range_start(resp) -> Optional[int]:
        """First byte offset a 206 response carries, from its Content-Range; None otherwise."""
        if resp.status_code != 206:
            return None
        content_range = resp.headers.get('content-range', '')
        try:
            return int(content_range.split(' ', 1)[1].split('-', 1)[0])
        except (IndexError, ValueError):
            return None

    # Bytes per slice sent back from the page: small enough that no single JS string
    # nears the engine's length limit or the renderer's heap, however large the video.
    BROWSER_FETCH_SLICE = 8 * 1024 * 1024

    async def _browser_fetch_bytes(self, bytes_url) -> bytes:
        """Fetch the video via an in-page fetch() so the request goes through the browser's network
        stack with the session cookies (credentials:'include'). The CDN authorizes by cookie, so
        credentials are required.

        The body stays in the page as a Uint8Array under a random key and comes back in
        base64 slices, so the whole file is never one string; the key is deleted afterwards.
        """
        # In the main world: the isolated one can't read the page's ArrayBuffer.
        evaluate = self.parent.tiktok_api.evaluate_main_world
        page = self.parent._page
        key = json.dumps(f"__pytok_bytes_{secrets.token_hex(8)}")
        fetch_js = (
            "(async () => { try {"
            f"  const resp = await fetch({json.dumps(bytes_url)}, {{ method: 'GET', credentials: 'include' }});"
            "  if (!resp.ok) return JSON.stringify({ok: false, status: resp.status});"
            "  const bytes = new Uint8Array(await resp.arrayBuffer());"
            f"  Object.defineProperty(window, {key}, {{ value: bytes, configurable: true }});"
            "  return JSON.stringify({ok: true, size: bytes.length});"
            "} catch (e) { return JSON.stringify({ok: false, error: String(e)}); } })()"
        )
        try:
            result = await evaluate(page, fetch_js)
            if not result:
                raise Exception("In-browser byte fetch returned no result")
            data = json.loads(result)
            if not data.get("ok"):
                raise Exception(f"In-browser byte fetch failed: status={data.get('status')} {data.get('error', '')}")
            size = data["size"]
            slices = []
            for offset in range(0, size, self.BROWSER_FETCH_SLICE):
                end = min(offset + self.BROWSER_FETCH_SLICE, size)
                b64 = await evaluate(page, (
                    "(() => {"
                    f"  const bytes = window[{key}];"
                    "  if (!bytes) return null;"
                    f"  const part = bytes.subarray({offset}, {end});"
                    "  let binary = ''; const chunk = 0x8000;"
                    "  for (let i = 0; i < part.length; i += chunk) {"
                    "    binary += String.fromCharCode.apply(null, part.subarray(i, i + chunk)); }"
                    "  return btoa(binary);"
                    "})()"
                ))
                if b64 is None:
                    raise Exception("In-browser byte fetch lost its buffer (page navigated?)")
                slices.append(base64.b64decode(b64))
            return b"".join(slices)
        finally:
            try:
                await asyncio.wait_for(evaluate(page, f"delete window[{key}]"), timeout=5)
            except Exception:
                pass  # best effort: a navigation frees the buffer anyway

    async def _get_comments_and_req(self, count):
        # get request
        data_request_path = "api/comment/list"
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
                self.parent.request_cache['comments'] = {'url': url}

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

            cookies = {c['name']: c['value'] for c in await self.parent._context.cookies()}

            # Get headers from the API client session
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

            self.parent.logger.debug(f"Making API request for comments with cursor={cursor}")
            try:
                res = await self.parent.tiktok_api.make_request(
                    url="https://www.tiktok.com/api/comment/list/",
                    params=params,
                )
            except Exception as e:
                self.parent.logger.warning(f"make_request failed for comments: {e}")
                raise exceptions.ApiFailedException(f"API request failed: {e}")

            if res is None:
                raise exceptions.ApiFailedException("API returned None response")

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
            try:
                comment_icon = await self._find_element_by_selector('[data-e2e="comment-icon"]', timeout=3)
                if comment_icon:
                    await comment_icon.click(timeout=3000)
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

            data_responses = await self.parent.process_pending_responses(data_request_path)
            data_responses = [resp for resp in data_responses if resp.get('url', '') not in processed_urls]

            if len(data_responses) == 0:
                if tries > 5:
                    logger.debug("Not sending anymore!")
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
                except Exception:
                    processed_urls.append(data_response.get('url', ''))

    async def _get_comments_via_requests(self, count, cursor, data_request):
        # Get the URL from the cached data (can be dict or object)
        cached_url = data_request.get('url', '') if isinstance(data_request, dict) else getattr(data_request, 'url', '')
        next_url = edit_url(cached_url, {'count': count, 'cursor': cursor, 'aweme_id': self.id})

        cookies = {c['name']: c['value'] for c in await self.parent._context.cookies()}

        # Get headers from the API client session
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
