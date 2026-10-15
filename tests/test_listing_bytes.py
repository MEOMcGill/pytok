import logging
from types import SimpleNamespace

import pytest

from pytok.api.hashtag import Hashtag
from pytok.api.search import Search
from pytok.api.sound import Sound

PARENT = SimpleNamespace(logger=logging.getLogger("PyTok.test"))


class _Video:
    def __init__(self, video_id, fail=False):
        self.id = video_id
        self.fail = fail
        self.fetched = False

    async def bytes(self):
        self.fetched = True
        if self.fail:
            raise Exception("CDN said no")
        return f"mp4-{self.id}".encode()


async def _listing(videos):
    for video in videos:
        yield video


def _hashtag(videos):
    hashtag = Hashtag(name="funny", parent=PARENT)

    def iter_videos(count, offset, prefer_scraping):
        return _listing(videos)
    hashtag._iter_videos = iter_videos
    return hashtag


def _sound(videos):
    sound = Sound(id="1", parent=PARENT)

    def iter_videos(count, offset, prefer_scraping):
        return _listing(videos)
    sound._iter_videos = iter_videos
    return sound


def _search(videos):
    search = Search("funny", parent=PARENT)

    def search_type(obj_type, count=28, offset=0, **kwargs):
        assert obj_type == "item"
        return _listing(videos)
    search.search_type = search_type
    return search


@pytest.mark.parametrize("make", [_hashtag, _sound, _search])
async def test_get_bytes_downloads_each_video(make):
    videos = [_Video(1), _Video(2, fail=True), _Video(3)]
    got = [v async for v in make(videos).videos(get_bytes=True)]
    assert [v.video_bytes for v in got] == [b"mp4-1", None, b"mp4-3"]


@pytest.mark.parametrize("make", [_hashtag, _sound, _search])
async def test_get_bytes_predicate_skips_videos(make):
    videos = [_Video(1), _Video(2)]

    async def wanted(video):
        return video.id != 2
    got = [v async for v in make(videos).videos(get_bytes=wanted)]
    assert [v.video_bytes for v in got] == [b"mp4-1", None]
    assert not videos[1].fetched


@pytest.mark.parametrize("make", [_hashtag, _sound, _search])
async def test_without_get_bytes_nothing_is_downloaded(make):
    videos = [_Video(1)]
    got = [v async for v in make(videos).videos()]
    assert got[0].video_bytes is None
    assert not videos[0].fetched
