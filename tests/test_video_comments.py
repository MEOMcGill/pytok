import logging
from types import SimpleNamespace

import pytest

from pytok import exceptions
from pytok.api import video as video_module
from pytok.api.video import COMMENT_LIST_URL, COMMENT_PAGE_SIZE, Video


class _FakeApi:
    """make_request over canned comment pages, keyed by cursor; no template until `fill()` runs."""

    def __init__(self, pages, template=True):
        self.pages = pages
        self.template = template
        self.requests = []

    def get_cached_api_params(self, url):
        return {"msToken": "x"} if self.template and url == COMMENT_LIST_URL else None

    async def make_request(self, url, params):
        if not self.template:
            raise exceptions.NoTemplateException("No param template captured yet for 'api/comment/list'")
        self.requests.append(dict(params))
        return self.pages[params["cursor"]]


def _video(api):
    parent = SimpleNamespace(logger=logging.getLogger("PyTok.test"), tiktok_api=api, navigations=[])

    async def navigate(url, **_):
        parent.navigations.append(url)
        api.template = True   # the page's own comment request fills the template

    async def request_delay():
        return None

    parent.navigate = navigate
    parent.request_delay = request_delay
    return Video(id="123", username="someone", parent=parent), parent


def _page(cids, cursor, has_more):
    return {"status_code": 0, "comments": [{"cid": c} for c in cids], "cursor": cursor, "has_more": has_more}


async def _cids(video, **kwargs):
    return [c["cid"] async for c in video._comments_api(fetch_replies=False, **kwargs)]


async def test_pages_are_asked_for_at_the_largest_size_tiktok_serves():
    api = _FakeApi({0: _page(["a", "b"], 50, 1), 50: _page(["c"], 100, 0)})
    video, _ = _video(api)
    assert await _cids(video, count=None) == ["a", "b", "c"]
    assert [r["count"] for r in api.requests] == [COMMENT_PAGE_SIZE] * 2
    assert [r["cursor"] for r in api.requests] == [0, 50]


async def test_a_missing_template_opens_the_video_page_and_retries():
    api = _FakeApi({0: _page(["a", "b"], 50, 0)}, template=False)
    video, parent = _video(api)
    assert await _cids(video, count=10) == ["a", "b"]
    assert parent.navigations == ["https://www.tiktok.com/@someone/video/123"]


async def test_a_page_that_never_requests_comments_fails_the_api_route(monkeypatch):
    monkeypatch.setattr(video_module, "COMMENT_TEMPLATE_WAIT", 0)
    api = _FakeApi({}, template=False)
    video, parent = _video(api)

    async def navigate(url, **_):
        parent.navigations.append(url)   # the template stays empty

    parent.navigate = navigate
    with pytest.raises(exceptions.ApiFailedException):
        await _cids(video, count=10)
