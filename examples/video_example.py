"""Fetch a video's metadata and related videos as a pooled account.

Run login_example.py (or the accounts CLI) once first to register + log in an
account.
"""

import asyncio
import json

from pytok.tiktok import PyTok
from pytok.accounts import AccountsPool

username = 'therock'
id = '7296444945991224622'


async def main():
    pool = AccountsPool()

    async with await PyTok.from_pool(pool) as api:
        video = api.video(username=username, id=id)

        video_data = await video.info()
        related_videos = []
        async for related_video in video.related_videos():
            related_videos.append(related_video)

        with open("out.json", "w") as out_file:
            json.dump(video_data, out_file)

        with open("related.json", "w") as out_file:
            json.dump(list(related_videos), out_file)


if __name__ == "__main__":
    asyncio.run(main())
