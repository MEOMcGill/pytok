import asyncio
import contextlib

from pytok.tiktok import PyTok
from pytok.accounts import AccountsPool
from pytok.exceptions import ApiFailedException, NoContentException

hashtag_name = 'funny'
missing_hashtag_name = 'awndoanwoidnawoidnaw'

COUNT = 25


@contextlib.asynccontextmanager
async def raises(exc_type):
    """Assert the block raises exc_type (pytest isn't a dependency of this env)."""
    try:
        yield
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}, nothing was raised")


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


async def test_missing_hashtag_raises_no_content():
    """A hashtag with no results must raise NoContentException on both routes."""
    pool = AccountsPool()
    async with await PyTok.from_pool(pool) as api:
        # The API route only runs once a param template for challenge/detail has
        # been captured off the wire, which the first (real) hashtag page load
        # does. Without this the API calls below would fail as NoTemplateException.
        # The first page load of a session often renders no video grid, so retry.
        for attempt in range(3):
            try:
                await api.hashtag(name=hashtag_name).info()
                break
            except ApiFailedException:
                if attempt == 2:
                    raise

        async with raises(NoContentException):
            await api.hashtag(name=missing_hashtag_name)._info_full_api()

        async with raises(NoContentException):
            await api.hashtag(name=missing_hashtag_name)._info_full_scrape()

        # The public entry points, which pick a route themselves.
        async with raises(NoContentException):
            await api.hashtag(name=missing_hashtag_name).info()

        async with raises(NoContentException):
            async for _ in api.hashtag(name=missing_hashtag_name).videos(count=5):
                pass


if __name__ == '__main__':
    asyncio.run(test_hashtag_videos())
    asyncio.run(test_missing_hashtag_raises_no_content())
