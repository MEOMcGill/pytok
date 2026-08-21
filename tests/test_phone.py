"""Tests for the phone (adb + CDP) backend.

The parsing tests run anywhere. The live test needs a handset, so it is opt-in::

    PYTOK_PHONE_SERIAL=R5CY23A5E7Y PYTOK_PHONE_SSH=meo-laptop \
        PYTOK_ADB='C:\\platform-tools\\adb.exe' pytest tests/test_phone.py
"""

import json
import os

import pytest

from pytok.exceptions import ApiFailedException, NotFoundException
from pytok.phone import PhoneTikTok


def _state(detail, text="") -> dict:
    """A `_load_profile` page state carrying the given `webapp.user-detail` scope."""
    return {
        "rehydration": json.dumps({"__DEFAULT_SCOPE__": {"webapp.user-detail": detail}}),
        "text": text,
        "captcha": False,
    }


def test_user_from_rehydration_merges_user_and_stats():
    """The returned dict is `{**user, **stats}` -- the shape `User.info()` gives callers."""
    state = _state({
        "statusCode": 0,
        "userInfo": {
            "user": {"id": "5831967", "uniqueId": "therock", "nickname": "The Rock"},
            "stats": {"followerCount": 79_700_000, "videoCount": 558},
        },
    })

    user = PhoneTikTok._user_from_rehydration(state, "therock")

    assert user["id"] == "5831967"
    assert user["uniqueId"] == "therock"
    assert user["followerCount"] == 79_700_000
    assert user["videoCount"] == 558


@pytest.mark.parametrize("status_code", [10202, 10221, 100002])
def test_user_from_rehydration_missing_account(status_code):
    """TikTok's "no such user" codes are a fact about the handle, not a failed scrape."""
    with pytest.raises(NotFoundException):
        PhoneTikTok._user_from_rehydration(_state({"statusCode": status_code}), "nobody")


def test_user_from_rehydration_other_status_is_a_failure():
    with pytest.raises(ApiFailedException):
        PhoneTikTok._user_from_rehydration(_state({"statusCode": 10101}), "someone")


def test_user_from_rehydration_no_blob_reads_the_page_text():
    """A profile page with no payload is ambiguous, so the visible text breaks the tie."""
    with pytest.raises(NotFoundException):
        PhoneTikTok._user_from_rehydration(
            {"rehydration": None, "text": "Couldn't find this account"}, "nobody")

    with pytest.raises(ApiFailedException):
        PhoneTikTok._user_from_rehydration(
            {"rehydration": None, "text": "Something else entirely"}, "someone")


def test_user_from_rehydration_rejects_a_payload_with_no_user():
    """statusCode 0 but no user id: a degraded response, not an account with no data."""
    with pytest.raises(ApiFailedException):
        PhoneTikTok._user_from_rehydration(
            _state({"statusCode": 0, "userInfo": {"user": {}, "stats": {}}}), "someone")


def test_user_from_rehydration_malformed_json():
    with pytest.raises(ApiFailedException):
        PhoneTikTok._user_from_rehydration(
            {"rehydration": "{not json", "text": ""}, "someone")


@pytest.mark.skipif(not os.getenv("PYTOK_PHONE_SERIAL"),
                    reason="needs a phone; set PYTOK_PHONE_SERIAL")
async def test_live_phone_user_info_and_videos():
    from pytok.utils import get_video_df

    async with PhoneTikTok(os.environ["PYTOK_PHONE_SERIAL"]) as phone:
        user = await phone.user_info("therock")
        assert user["uniqueId"] == "therock"
        assert user["followerCount"] > 0

        videos = await phone.user_videos("therock")
        assert videos, "mobile web returned no videos for a profile that has them"
        assert all(v.get("id") for v in videos)
        # The raw itemList dicts must stay compatible with pytok's dataframe helpers.
        assert len(get_video_df(videos)) == len(videos)


if __name__ == "__main__":
    pytest.main([__file__])
