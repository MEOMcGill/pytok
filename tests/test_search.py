import asyncio

import pytest

from pytok.accounts import AccountsPool
from pytok.tiktok import PyTok

pytestmark = pytest.mark.live

search_term = 'news'

COUNT = 25


async def test_search_videos():
    # Scrape as a logged-in pooled account: TikTok's search endpoint returns
    # empty responses to anonymous sessions. Register + log in an account once
    # with `python -m pytok.accounts.cli add|login` first.
    pool = AccountsPool()
    async with await PyTok.from_pool(pool) as api:
        video_ids = set()
        async for video in api.search(search_term).videos(count=COUNT):
            video_ids.add(video.id)

        # No duplicates, whichever route (API pagination or page scrolling) served
        # the results.
        assert len(video_ids) >= COUNT


async def test_search_users():
    pool = AccountsPool()
    async with await PyTok.from_pool(pool) as api:
        usernames = set()
        async for user in api.search(search_term).users(count=COUNT):
            usernames.add(user.username)

        assert len(usernames) >= COUNT


if __name__ == '__main__':
    asyncio.run(test_search_videos())
