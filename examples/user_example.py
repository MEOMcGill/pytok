"""Fetch a user's videos as a logged-in account from the pool.

Run login_example.py (or the accounts CLI) once first to register + log in an
account. PyTok.from_pool acquires the least-recently-used available account and
comes up already authenticated from its persistent profile.
"""

import asyncio
import json
import logging

from pytok.tiktok import PyTok
from pytok.accounts import AccountsPool

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


async def main():
    users = ['therock']
    pool = AccountsPool()

    async with await PyTok.from_pool(pool, logging_level=logging.INFO) as api:
        for username in users:
            user = api.user(username=username)
            user_data = await user.info()

            videos = []
            async for video in user.videos(count=30):
                video_data = await video.info()
                videos.append(video_data)

            assert len(videos) > 0, "No videos found"
            print(f"Fetched {len(videos)} videos for user {username}")
            with open("out.json", "w") as f:
                json.dump(videos, f)


if __name__ == "__main__":
    asyncio.run(main())
