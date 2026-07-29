import asyncio

from pytok.tiktok import PyTok
from pytok.accounts import AccountsPool

hashtag_name = 'funny'

COUNT = 25


async def test_hashtag_videos():
    # Scrape as a logged-in pooled account: TikTok's challenge/item_list endpoint
    # returns empty responses to anonymous sessions. Register + log in an account
    # once with `python -m pytok.accounts.cli add|login` first.
    pool = AccountsPool()
    async with await PyTok.from_pool(pool) as api:
        hashtag = api.hashtag(name=hashtag_name)
        hashtag_data = await hashtag.info()
        assert hashtag_data.get('challenge', {}).get('id') or hashtag_data.get('id')

        video_ids = set()
        async for video in hashtag.videos(count=COUNT):
            video_ids.add(video.id)

        # No duplicates, whichever route (API pagination or page scrolling) served
        # the results.
        assert len(video_ids) >= COUNT


if __name__ == '__main__':
    asyncio.run(test_hashtag_videos())
