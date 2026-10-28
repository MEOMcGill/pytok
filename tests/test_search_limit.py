import json
import logging
from types import SimpleNamespace

import pytest

from pytok.api.search import Search
from pytok.exceptions import ApiFailedException, SearchLimitException
from pytok.tiktok_api import TikTokApiClient

CAPPED = {
    "status_code": 2484,
    "status_msg": "You have reached the maximum number of searched today.",
    "log_pb": {"impr_id": "1"},
}

SEARCH_URL = "https://www.tiktok.com/api/search/item/full/"


def _client(body):
    client = TikTokApiClient(logger_name="PyTok.test")
    session = SimpleNamespace(is_valid=True)
    calls = []

    async def get_valid_session_index(**kwargs):
        return 0, session

    async def ensure_signer_loaded(session):
        pass

    async def run_fetch_script(url, **kwargs):
        calls.append(url)
        return json.dumps(body)

    client._get_valid_session_index = get_valid_session_index
    client._ensure_signer_loaded = ensure_signer_loaded
    client.run_fetch_script = run_fetch_script
    client.get_cached_api_params = lambda url: {"aid": "1988"}
    return client, session, calls


async def test_make_request_raises_on_search_limit_without_retrying():
    client, session, calls = _client(CAPPED)

    with pytest.raises(SearchLimitException):
        await client.make_request(url=SEARCH_URL, params={"keyword": "x"})

    assert len(calls) == 1
    assert session.is_valid


async def test_make_request_returns_other_responses():
    client, _, _ = _client({"status_code": 0, "item_list": []})

    assert await client.make_request(url=SEARCH_URL, params={"keyword": "x"}) == {
        "status_code": 0, "item_list": []}


def test_search_limit_is_an_api_failure():
    # Workers rest and rotate an account on ApiFailedException.
    assert issubclass(SearchLimitException, ApiFailedException)


def _search(make_request):
    loaded = []

    async def request_delay():
        pass

    parent = SimpleNamespace(
        logger=logging.getLogger("PyTok.test"),
        tiktok_api=SimpleNamespace(make_request=make_request),
        request_delay=request_delay,
    )
    search = Search("funny", parent=parent)

    async def load_search_page(obj_type):
        loaded.append(obj_type)
    search._load_search_page = load_search_page
    return search, loaded


async def test_search_does_not_fall_back_to_the_page_when_capped():
    async def make_request(**kwargs):
        raise SearchLimitException("capped")
    search, loaded = _search(make_request)

    with pytest.raises(SearchLimitException):
        async for _ in search.videos(count=10):
            pass

    assert loaded == []


async def test_search_still_falls_back_on_other_api_failures():
    async def make_request(**kwargs):
        raise ApiFailedException("empty")
    search, loaded = _search(make_request)

    async def harvest(obj_type):
        return [], False, 0, ""
    search._harvest_page_results = harvest

    async for _ in search.videos(count=10):
        pass

    assert loaded == ["item"]


def test_capped_page_response_raises():
    search, _ = _search(None)

    with pytest.raises(SearchLimitException):
        search._parse_response({"body": json.dumps(CAPPED)})

    assert search._parse_response({"body": json.dumps({"status_code": 0})}) == {"status_code": 0}
