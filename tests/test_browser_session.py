"""Browser-session plumbing that doesn't need a browser: request matching, header
capture, cookie round-trips and the profile migration.

Offline: no browser, no network. Async bodies are driven with asyncio.run.
"""

import asyncio
import os
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest

from pytok.accounts import AccountsPool
from pytok.tiktok import PyTok
from pytok.tiktok_api import TikTokApiClient

ITEM_LIST = "https://www.tiktok.com/api/post/item_list/"


def test_own_fetch_is_recognised_after_the_sdk_signs_it():
    client = TikTokApiClient()
    ours = f"{ITEM_LIST}?aid=1988&secUid=MS4w_abc&cursor=0"
    client._inflight_fetch_urls.add(ours)

    # What goes out on the wire: re-encoded, with the SDK's params appended.
    wire = f"{ITEM_LIST}?aid=1988&secUid=MS4w%5Fabc&cursor=0&msToken=t&X-Bogus=b&X-Gnarly=g"
    assert client.is_self_issued(wire)
    assert not client.is_self_issued(wire.replace("cursor=0", "cursor=16"))
    assert not client.is_self_issued(wire.replace("post/item_list", "user/detail"))


# PyTok.__del__ is async, so collecting an unstarted PyTok warns.
pytestmark = pytest.mark.filterwarnings("ignore:coroutine 'PyTok.__del__' was never awaited")


def _unstarted_pytok():
    api = PyTok()
    api._captured_request_headers = None
    api._pending_requests = {}
    return api


def test_captured_headers_leave_out_what_only_that_request_can_carry():
    api = _unstarted_pytok()
    request = SimpleNamespace(url="https://www.tiktok.com/", headers={
        "host": "www.tiktok.com", "cookie": "sid=1", "connection": "keep-alive",
        "user-agent": "Firefox", "accept-language": "en-US",
    })
    api._on_request(request)
    assert api._captured_request_headers == {"user-agent": "Firefox", "accept-language": "en-US"}


class _FakeContext:
    def __init__(self):
        self.added = []

    async def add_cookies(self, cookies):
        self.added.extend(cookies)


def test_injected_cookie_keeps_an_unset_same_site_unset():
    api = _unstarted_pytok()
    api._context = _FakeContext()
    asyncio.run(api._inject_cookies([
        {"name": "sessionid", "value": "s", "domain": ".tiktok.com", "path": "/",
         "secure": True, "httpOnly": True, "sameSite": None, "expires": 1900000000.5},
        {"name": "tt_csrf_token", "value": "c", "domain": ".tiktok.com", "sameSite": "lax"},
        {"name": "ttwid", "value": "w", "sameSite": "no_restriction"},
        {"name": "broken", "value": None},
    ]))
    by_name = {c["name"]: c for c in api._context.added}
    assert set(by_name) == {"sessionid", "tt_csrf_token", "ttwid"}
    assert "sameSite" not in by_name["sessionid"]
    assert by_name["sessionid"]["expires"] == 1900000000.5
    assert by_name["tt_csrf_token"]["sameSite"] == "Lax"
    # a domainless cookie is anchored to a url instead
    assert by_name["ttwid"] == {"name": "ttwid", "value": "w", "url": "https://www.tiktok.com", "sameSite": "None"}


def test_migration_forgets_chrome_profile_dirs():
    db_file = os.path.join(tempfile.mkdtemp(), "accounts.db")
    with sqlite3.connect(db_file) as db:
        db.execute("CREATE TABLE accounts (username TEXT, profile_dir TEXT, active BOOLEAN DEFAULT 0, "
                   "locks TEXT DEFAULT '{}', cookies TEXT DEFAULT '[]')")
        db.execute("INSERT INTO accounts (username, profile_dir) VALUES ('a', '/old/chrome/profile')")
        db.execute("PRAGMA user_version = 1")

    async def stored_dirs():
        pool = AccountsPool(db_file=db_file)
        await pool.get_active_accounts()  # any query runs the migrations
        with sqlite3.connect(db_file) as db:
            return [r[0] for r in db.execute("SELECT profile_dir FROM accounts")]

    assert asyncio.run(stored_dirs()) == [None]
