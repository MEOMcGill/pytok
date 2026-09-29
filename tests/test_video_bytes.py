import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest

from pytok.api.video import Video
from pytok.tiktok import PyTok

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


class _Parent:
    """The slice of PyTok that Video.bytes touches, with the media client serving `handler`."""

    def __init__(self, handler, cookies=None, cookie_delay=0):
        self.logger = logging.getLogger("PyTok.test")
        self.request_cache = {}
        self.tiktok_api = SimpleNamespace(
            _get_session=lambda: (0, SimpleNamespace(headers={"User-Agent": "test"})))
        self.cookie_reads = 0
        self.browser_fetches = 0
        self._cookies = cookies or [{"name": "sessionid", "value": "abc", "domain": ".tiktok.com", "path": "/"}]
        self._cookie_delay = cookie_delay
        self._media_http_client = httpx.AsyncClient(
            follow_redirects=True, transport=httpx.MockTransport(handler))
        self._media_cookies_read_at = None
        self._context = SimpleNamespace(cookies=self._read_cookies)

    async def _read_cookies(self):
        self.cookie_reads += 1
        await asyncio.sleep(self._cookie_delay)
        return self._cookies

    _media_client = PyTok._media_client
    _expire_media_cookies = PyTok._expire_media_cookies
    MEDIA_COOKIE_TTL = PyTok.MEDIA_COOKIE_TTL


def _video(parent, browser_result=MP4):
    video = Video(id="1", data={"id": "1", "video": {
        "playAddr": "https://v16.tiktok.com/play", "downloadAddr": "https://v16.tiktok.com/download"}},
        parent=parent)
    video.get_responses = lambda path: []

    async def browser_fetch(url):
        parent.browser_fetches += 1
        return browser_result

    video._browser_fetch_bytes = browser_fetch
    return video


async def test_downloads_with_browser_cookies_and_reads_them_once():
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, content=MP4)

    parent = _Parent(handler)
    assert await _video(parent).bytes(timeout=5) == MP4
    assert await _video(parent).bytes(timeout=5) == MP4
    assert seen == ["sessionid=abc", "sessionid=abc"]
    assert parent.cookie_reads == 1


async def test_rejection_refreshes_cookies_and_falls_back_to_browser():
    parent = _Parent(lambda request: httpx.Response(403))
    assert await _video(parent).bytes(timeout=5) == MP4
    assert parent.browser_fetches == 1
    # a 403 expires the cached cookies, so the second URL re-reads them
    assert parent.cookie_reads == 2


async def test_network_failure_skips_browser_fallback():
    def handler(request):
        raise httpx.ReadTimeout("no data", request=request)

    parent = _Parent(handler)
    with pytest.raises(Exception, match="httpx/playAddr: ReadTimeout.*httpx/downloadAddr: ReadTimeout"):
        await _video(parent).bytes(timeout=5)
    assert parent.browser_fetches == 0


async def test_unanswered_cookie_read_fails_fast():
    parent = _Parent(lambda request: httpx.Response(200, content=MP4), cookie_delay=5)
    with pytest.raises(Exception, match="reading the browser's cookies timed out"):
        await _video(parent).bytes(timeout=0.1)
    assert parent.cookie_reads == 1
    assert parent.browser_fetches == 0
