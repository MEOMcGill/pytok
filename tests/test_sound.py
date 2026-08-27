import asyncio

import pytest

from pytok.accounts import AccountsPool
from pytok.tiktok import PyTok

pytestmark = pytest.mark.live

# A sound with plenty of videos. Sounds do get taken down — if this test starts
# returning nothing, grab a fresh id from any video's `music` dict.
sound_id = '7659457659034175489'

COUNT = 25


async def test_sound_videos():
    # Scrape as a logged-in pooled account: TikTok's music/item_list endpoint
    # returns empty responses to anonymous sessions. Register + log in an account
    # once with `python -m pytok.accounts.cli add|login` first.
    pool = AccountsPool()
    async with await PyTok.from_pool(pool) as api:
        sound = api.sound(id=sound_id)

        video_ids = set()
        async for video in sound.videos(count=COUNT):
            video_ids.add(video.id)

        # No duplicates, whichever route (API pagination or page scrolling) served
        # the results.
        assert len(video_ids) >= COUNT


async def test_sound_info():
    pool = AccountsPool()
    async with await PyTok.from_pool(pool) as api:
        sound = api.sound(id=sound_id)
        sound_data = await sound.info()

        assert sound_data.get('music', {}).get('id') == sound_id
        assert sound.title


if __name__ == '__main__':
    asyncio.run(test_sound_videos())
