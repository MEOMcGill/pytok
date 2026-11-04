import logging
from types import SimpleNamespace

from pytok.api.for_you import STALL_ROUNDS, ForYou
from pytok.exceptions import ApiFailedException


def _feed(first, api_batches=(), page_batches=(), prefer_scraping=False):
    """A ForYou whose page loads `first`, then serves api/page batches in order.

    A batch that is an exception instance is raised instead of returned.
    """
    parent = SimpleNamespace(logger=logging.getLogger("PyTok.test"),
                             video=lambda data: SimpleNamespace(id=data["id"]))
    feed = ForYou(parent=parent)
    api, page = list(api_batches), list(page_batches)
    calls = []

    async def load_feed():
        return [{"id": i} for i in first]

    async def api_batch():
        calls.append("api")
        batch = api.pop(0) if api else []
        if isinstance(batch, Exception):
            raise batch
        return [{"id": i} for i in batch]

    async def advance_feed():
        calls.append("page")
        return [{"id": i} for i in (page.pop(0) if page else [])]

    feed._load_feed = load_feed
    feed._api_batch = api_batch
    feed._advance_feed = advance_feed
    return feed, calls


async def _ids(feed, count=100, prefer_scraping=False):
    return [v.id async for v in feed._iter_videos(count=count, prefer_scraping=prefer_scraping)]


async def test_batches_are_deduplicated_and_count_is_honoured():
    feed, _ = _feed([1, 2, 3], api_batches=[[3, 4, 5], [5, 6, 7]])
    assert await _ids(feed, count=6) == [1, 2, 3, 4, 5, 6]


async def test_walk_ends_after_rounds_with_nothing_new():
    feed, calls = _feed([1, 2], api_batches=[[2], [1, 2], []])
    assert await _ids(feed) == [1, 2]
    assert len(calls) == STALL_ROUNDS


async def test_refused_api_batch_falls_back_to_moving_the_page():
    feed, calls = _feed([1], api_batches=[ApiFailedException("refused"), [3]], page_batches=[[2]])
    assert await _ids(feed, count=3) == [1, 2, 3]
    assert calls == ["api", "page", "api"]


async def test_prefer_scraping_never_replays_the_request():
    feed, calls = _feed([1], api_batches=[[9]], page_batches=[[2], [3]])
    assert await _ids(feed, count=3, prefer_scraping=True) == [1, 2, 3]
    assert "api" not in calls


async def test_a_video_listed_twice_in_one_batch_is_yielded_once():
    feed, _ = _feed([1, 2, 2, 3], api_batches=[[4, 4, 5]])
    assert await _ids(feed, count=5) == [1, 2, 3, 4, 5]
