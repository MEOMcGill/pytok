import asyncio

import pytest

from pytok.accounts import AccountsPool
from pytok.tiktok import PyTok

pytestmark = pytest.mark.live

# username = "brianjordanalvarez"
username = 'marierenaudstab'

COUNT = 100


async def test_user_videos():
    # Scrape as a logged-in pooled account: anonymous sessions get empty
    # responses from most TikTok endpoints now. Register + log in an account once
    # with `python -m pytok.accounts.cli add|login` first.
    pool = AccountsPool()
    async with await PyTok.from_pool(pool) as api:
        user = api.user(username=username)
        await user.info()
        count = 0
        async for _ in api.user(username=username).videos(count=COUNT):
            count += 1

        assert count >= COUNT


if __name__ == '__main__':
    asyncio.run(test_user_videos())
