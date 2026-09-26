import logging
from types import SimpleNamespace

from pytok.api.user import User


def _user(**kwargs):
    user = User(parent=SimpleNamespace(logger=logging.getLogger("PyTok.test")), **kwargs)
    listed_with = []

    async def info(**_):
        user.sec_uid = "SEC"
        user.as_dict = {"videoCount": 1}

    async def inner(**_):
        listed_with.append(user.sec_uid)
        user._listing_exhausted = True
        yield "video"

    user.info = info
    user._iter_videos_inner = inner
    return user, listed_with


async def test_videos_looks_up_sec_uid_when_missing():
    user, listed_with = _user(username="someone")
    assert [v async for v in user._iter_videos()] == ["video"]
    assert listed_with == ["SEC"]


async def test_videos_skips_lookup_when_sec_uid_given():
    user, listed_with = _user(username="someone", user_id="1", sec_uid="GIVEN")
    assert [v async for v in user._iter_videos()] == ["video"]
    assert listed_with == ["GIVEN"]
