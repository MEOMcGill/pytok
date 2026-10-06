import logging
from types import SimpleNamespace

from pytok.api.user import User


def _video(video_id):
    return SimpleNamespace(id=video_id)


def _user(initial, api_pages, finished=False, cursor="C1"):
    """A User whose profile page held `initial` and whose item_list walk serves `api_pages`."""
    user = User(parent=SimpleNamespace(logger=logging.getLogger("PyTok.test")),
                username="someone", user_id="1", sec_uid="SEC")
    user._used_api_for_info = False
    api_calls = []

    async def get_initial_videos(count):
        return [_video(i) for i in initial], finished, cursor

    async def get_videos_api(count=None, cursor=0, **_):
        api_calls.append(cursor)
        for i in api_pages.get(cursor, []):
            yield _video(i)

    user._get_initial_videos = get_initial_videos
    user._get_videos_api = get_videos_api
    return user, api_calls


async def _ids(user, **kwargs):
    return [v.id async for v in user._iter_videos_inner(**kwargs)]


async def test_api_walk_resumes_from_the_page_cursor():
    user, api_calls = _user(initial=[1, 2, 3], api_pages={0: [1, 2, 3, 4], "C1": [4, 5, 6]})
    assert await _ids(user) == [1, 2, 3, 4, 5, 6]
    assert api_calls == ["C1"]


async def test_repeats_across_the_page_boundary_are_dropped_without_using_up_count():
    user, _ = _user(initial=[1, 2, 3], api_pages={"C1": [3, 4, 5, 6]})
    assert await _ids(user, count=5) == [1, 2, 3, 4, 5]


async def test_count_reached_on_the_page_skips_the_api():
    user, api_calls = _user(initial=[1, 2, 3], api_pages={"C1": [4]})
    assert await _ids(user, count=2) == [1, 2]
    assert api_calls == []


async def test_finished_page_ends_the_walk():
    user, api_calls = _user(initial=[1, 2], api_pages={"C1": [3]}, finished=True)
    assert await _ids(user) == [1, 2]
    assert api_calls == []
    assert user._listing_exhausted
