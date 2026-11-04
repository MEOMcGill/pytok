import asyncio
import http.server
import json
import logging
import shutil
import threading
from types import SimpleNamespace

import httpx
import pytest

from pytok.api.video import Video
from pytok.tiktok import PyTok

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


class _Parent:
    """The slice of PyTok that Video.media touches, with the media client serving `handler`."""

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
    assert (await _video(parent).media(timeout=5)).video == MP4
    assert (await _video(parent).media(timeout=5)).video == MP4
    assert seen == ["sessionid=abc", "sessionid=abc"]
    assert parent.cookie_reads == 1


async def test_rejection_refreshes_cookies_and_falls_back_to_browser():
    parent = _Parent(lambda request: httpx.Response(403))
    assert (await _video(parent).media(timeout=5)).video == MP4
    assert parent.browser_fetches == 1
    # a 403 expires the cached cookies, so the second URL re-reads them
    assert parent.cookie_reads == 2


async def test_network_failure_skips_browser_fallback():
    def handler(request):
        raise httpx.ReadTimeout("no data", request=request)

    parent = _Parent(handler)
    with pytest.raises(Exception, match="httpx/playAddr: ReadTimeout.*httpx/downloadAddr: ReadTimeout"):
        await _video(parent).media(timeout=5)
    assert parent.browser_fetches == 0


def _cutting_cdn(body, cut_every):
    """A CDN that serves `body` honouring Range, but drops each connection after `cut_every` bytes."""
    requests = []

    def handler(request):
        start = 0
        if "range" in request.headers:
            start = int(request.headers["range"].removeprefix("bytes=").split("-")[0])
        requests.append((start, request.headers.get("accept-encoding")))
        part = body[start:]

        async def stream():
            yield part[:cut_every]
            if len(part) > cut_every:
                raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

        status, headers = 200, {"content-length": str(len(part))}
        if start:
            status = 206
            headers["content-range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
        return httpx.Response(status, headers=headers, stream=_AsyncStream(stream()))

    return handler, requests


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, gen):
        self._gen = gen

    async def __aiter__(self):
        async for chunk in self._gen:
            yield chunk


async def test_cut_download_resumes_from_where_it_stopped():
    body = MP4 + bytes(range(256)) * 40
    handler, requests = _cutting_cdn(body, cut_every=1000)
    parent = _Parent(handler)
    assert (await _video(parent).media(timeout=5)).video == body
    assert [start for start, _ in requests] == list(range(0, len(body), 1000))
    assert all(encoding == "identity" for _, encoding in requests)
    assert parent.browser_fetches == 0


async def test_resume_ignored_by_cdn_fails_rather_than_splicing():
    body = MP4 + b"\x01" * 5000

    def handler(request):
        async def stream():
            yield body[:1000]
            raise httpx.RemoteProtocolError("peer closed connection")
        # always the whole file from byte 0, whatever Range asked for
        return httpx.Response(200, stream=_AsyncStream(stream()))

    parent = _Parent(handler)
    with pytest.raises(Exception, match="resuming at byte 1000 got 200"):
        await _video(parent).media(timeout=5)


async def test_unanswered_cookie_read_fails_fast():
    parent = _Parent(lambda request: httpx.Response(200, content=MP4), cookie_delay=5)
    with pytest.raises(Exception, match="reading the browser's cookies timed out"):
        await _video(parent).media(timeout=0.1)
    assert parent.cookie_reads == 1
    assert parent.browser_fetches == 0


_NODE_EVAL = r"""
globalThis.window = globalThis;
const rl = require('readline').createInterface({input: process.stdin});
rl.on('line', async (line) => {
  let out;
  try { out = {result: await (0, eval)(JSON.parse(line))}; }
  catch (e) { out = {error: String(e)}; }
  process.stdout.write(JSON.stringify(out === undefined ? null : out) + '\n');
});
"""


class _NodePage:
    """Runs page-world expressions in a Node process, standing in for the browser page."""

    async def __aenter__(self):
        self.proc = await asyncio.create_subprocess_exec(
            "node", "-e", _NODE_EVAL, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024)  # a full-size slice arrives as one line
        return self

    async def __aexit__(self, *exc):
        self.proc.stdin.close()
        await self.proc.wait()

    async def evaluate(self, page, expression):
        self.proc.stdin.write(json.dumps(expression).encode() + b"\n")
        out = json.loads(await self.proc.stdout.readline())
        if "error" in out:
            raise Exception(out["error"])
        return out["result"]


@pytest.fixture
def cdn():
    """A local CDN serving a video at /video and a 404 elsewhere."""
    body = MP4 + bytes(range(256)) * 40 + b"tail"  # not a multiple of the slice size or of 3

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/video"
            self.send_response(200 if ok else 404)
            self.send_header("Content-Length", str(len(body) if ok else 0))
            self.end_headers()
            if ok:
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", body
    server.shutdown()


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page script")
async def test_browser_fetch_reassembles_slices_and_frees_buffer(cdn, monkeypatch):
    base_url, body = cdn
    monkeypatch.setattr(Video, "BROWSER_FETCH_SLICE", 1000)
    async with _NodePage() as page:
        parent = SimpleNamespace(_page=None, tiktok_api=SimpleNamespace(evaluate_main_world=page.evaluate))
        video = Video(id="1", data={"id": "1"}, parent=parent)
        assert await video._browser_fetch_bytes(base_url + "/video") == body
        with pytest.raises(Exception, match="status=404"):
            await video._browser_fetch_bytes(base_url + "/missing")
        leftover = await page.evaluate(None, "Object.getOwnPropertyNames(window).filter(k => k.startsWith('__pytok'))")
        assert leftover == []


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


def _photo_post(parent, images, music=None):
    data = {"id": "1", "video": {"duration": 0}, "music": music or {},
            "imagePost": {"images": [{"imageURL": {"urlList": urls}} for urls in images]}}
    video = Video(id="1", data=data, parent=parent)
    video.get_responses = lambda path: []

    async def browser_fetch(url):
        parent.browser_fetches += 1
        return JPEG

    video._browser_fetch_bytes = browser_fetch
    return video


async def test_photo_post_downloads_images_in_order_and_its_sound(tmp_path):
    bodies = {"/a.jpeg": JPEG, "/b.png": PNG, "/music": MP3}
    parent = _Parent(lambda request: httpx.Response(200, content=bodies[request.url.path]))
    video = _photo_post(parent, [["https://p16.tiktokcdn.com/a.jpeg"], ["https://p16.tiktokcdn.com/b.png"]],
                        music={"playUrl": "https://sf16.tiktokcdn.com/music"})
    assert video.is_photo_post

    media = await video.media(timeout=5)
    assert media.is_photo_post and media.video is None
    assert media.images == [JPEG, PNG]
    assert media.audio == MP3
    assert [p.name for p in media.save(tmp_path)] == ["1_01.jpg", "1_02.png", "1.mp3"]
    assert (tmp_path / "1_02.png").read_bytes() == PNG


async def test_video_post_saves_its_mp4(tmp_path):
    parent = _Parent(lambda request: httpx.Response(200, content=MP4))
    media = await _video(parent).media(timeout=5)
    assert not media.is_photo_post
    assert media.images == [] and media.audio is None
    assert [p.name for p in media.save(tmp_path)] == ["1.mp4"]
    assert (tmp_path / "1.mp4").read_bytes() == MP4


async def test_photo_post_without_a_sound_url_has_no_audio():
    parent = _Parent(lambda request: httpx.Response(200, content=JPEG))
    media = await _photo_post(parent, [["https://p16.tiktokcdn.com/a.jpeg"]],
                              music={"id": "0", "title": "original sound"}).media(timeout=5)
    assert media.images == [JPEG]
    assert media.audio is None


async def test_photo_post_tries_the_next_host_then_the_browser():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, content=b"<html>blocked</html>")

    parent = _Parent(handler)
    video = _photo_post(parent, [["https://p16.tiktokcdn.com/a.jpeg", "https://p19.tiktokcdn.com/a.jpeg"]])
    media = await video.media(timeout=5)
    assert seen == ["p16.tiktokcdn.com", "p19.tiktokcdn.com"]
    assert media.images == [JPEG]
    assert parent.browser_fetches == 1


async def test_photo_post_image_that_never_arrives_fails_naming_it():
    parent = _Parent(lambda request: httpx.Response(200, content=b"<html>blocked</html>"))
    video = _photo_post(parent, [["https://p16.tiktokcdn.com/a.jpeg"]])
    video._browser_fetch_bytes = lambda url: _async(b"")
    with pytest.raises(Exception, match="image 1 of photo post 1.*markup/JSON, not an image"):
        await video.media(timeout=5)


async def test_post_without_any_media_raises_no_video():
    from pytok.exceptions import NoVideoException
    parent = _Parent(lambda request: httpx.Response(200))
    with pytest.raises(NoVideoException):
        await _photo_post(parent, []).media(timeout=5)
    with pytest.raises(NoVideoException):
        await Video(id="1", data={"id": "1", "video": {}}, parent=parent).media(timeout=5)


async def _async(value):
    return value
