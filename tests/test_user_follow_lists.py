import logging
from types import SimpleNamespace

import pytest

from pytok.api.user import FOLLOWERS_SCENE, FOLLOWING_SCENE, User
from pytok.exceptions import ApiFailedException, NoContentException, NoTemplateException


def _entry(i):
    return {"user": {"id": str(i), "uniqueId": f"u{i}", "secUid": f"s{i}"},
            "stats": {"followerCount": i}}


def _page(ids, has_more, min_cursor=0, **extra):
    return {"statusCode": 0, "userList": [_entry(i) for i in ids], "hasMore": has_more,
            "minCursor": min_cursor, **extra}


def _user(pages, as_dict=None):
    """A User whose api/user/list requests are answered from `pages`, in order.

    A page that is an exception instance is raised instead of returned.
    """
    requests = []
    pages = list(pages)

    async def make_request(url, params):
        requests.append(dict(params))
        page = pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    async def request_delay():
        pass

    parent = SimpleNamespace(
        logger=logging.getLogger("PyTok.test"),
        tiktok_api=SimpleNamespace(make_request=make_request),
        request_delay=request_delay,
    )
    parent.user = lambda data: User(data=data, parent=parent)
    user = User(username="someone", user_id="1", sec_uid="SEC", parent=parent)
    user.as_dict = as_dict or {}
    return user, requests


async def _names(iterator):
    return [u.username async for u in iterator]


async def test_walks_pages_by_min_cursor_until_has_more_is_false():
    user, requests = _user([_page([1, 2], True, 900), _page([3], False, 800)])
    assert await _names(user.followers()) == ["u1", "u2", "u3"]
    assert [(r["scene"], r["minCursor"]) for r in requests] == [(FOLLOWERS_SCENE, 0), (FOLLOWERS_SCENE, 900)]
    assert all(r["secUid"] == "SEC" for r in requests)


async def test_following_uses_its_own_scene():
    user, requests = _user([_page([1], False)])
    await _names(user.following())
    assert requests[0]["scene"] == FOLLOWING_SCENE


async def test_entries_carry_their_stats_and_ids():
    user, _ = _user([_page([7], False)])
    [follower] = [u async for u in user.followers()]
    assert (follower.user_id, follower.sec_uid) == ("7", "s7")
    assert follower.as_dict["followerCount"] == 7


async def test_count_stops_the_walk_without_another_request():
    user, requests = _user([_page([1, 2, 3], True, 900)])
    assert await _names(user.followers(count=2)) == ["u1", "u2"]
    assert len(requests) == 1


async def test_repeats_and_unnamed_placeholders_are_skipped():
    page = _page([1, 2], True, 900)
    page["userList"].append({"user": {"id": "9", "uniqueId": ""}})
    user, _ = _user([page, _page([2, 3], False)])
    assert await _names(user.followers()) == ["u1", "u2", "u3"]


async def test_empty_page_that_claims_more_is_a_failure_not_the_end():
    user, _ = _user([_page([1], True, 900), _page([], True, 800)])
    with pytest.raises(ApiFailedException):
        await _names(user.followers())


async def test_error_status_is_no_content():
    user, _ = _user([{"statusCode": 10222, "status_msg": "private"}])
    with pytest.raises(NoContentException):
        await _names(user.following())


async def test_zero_count_makes_no_request():
    user, requests = _user([], as_dict={"followingCount": 0})
    assert await _names(user.following()) == []
    assert requests == []


async def test_missing_template_is_captured_from_the_profile_then_retried():
    user, requests = _user([NoTemplateException("none"), _page([1], False)])
    captured = []

    async def capture(scene):
        captured.append(scene)

    user._capture_user_list_template = capture
    assert await _names(user.followers()) == ["u1"]
    assert captured == [FOLLOWERS_SCENE]
    assert len(requests) == 2
