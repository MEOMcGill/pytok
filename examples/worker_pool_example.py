"""Scrape concurrently across multiple accounts with a WorkerPool.

Each worker owns one account and its own Chrome session (isolated by the
account's profile dir), so N workers run N concurrent scraping sessions. Tasks
are plain async callables ``async def task(api: PyTok) -> result`` submitted to a
shared queue; the pool distributes them across workers and gathers the results.

Register + log in at least one account first (login_example.py or the accounts
CLI). With a single account this still works — you just get one worker.
"""

import asyncio
import logging

from pytok.accounts import AccountsPool, WorkerPool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


async def scrape_user_videos(api, handle, count=30):
    """One unit of work, run on whichever worker/account picks it up."""
    videos = []
    async for video in api.user(username=handle).videos(count=count):
        videos.append(await video.info())
    return handle, videos


async def main():
    pool = AccountsPool()
    handles = ["therock", "khaby.lame", "charlidamelio"]

    # max_workers is capped to the number of active accounts, so this uses as
    # many concurrent sessions as you have accounts (up to 3 here).
    async with WorkerPool(pool, max_workers=3) as wp:
        tasks = [lambda api, h=h: scrape_user_videos(api, h) for h in handles]
        results = await wp.run(tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            print(f"task failed: {r!r}")
        else:
            handle, videos = r
            print(f"@{handle}: {len(videos)} videos")


if __name__ == "__main__":
    asyncio.run(main())
