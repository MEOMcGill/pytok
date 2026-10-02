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
