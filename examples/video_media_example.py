"""Download a post's media as a pooled account: the MP4 of a video post, or the
images and sound of a photo/slideshow post.

Run login_example.py (or the accounts CLI) once first to register + log in an
account.
"""

import asyncio
import json

from pytok.accounts import AccountsPool
from pytok.tiktok import PyTok

username = 'therock'
id = '7296444945991224622'


async def main():
    pool = AccountsPool()

    async with await PyTok.from_pool(pool) as api:
        video = api.video(username=username, id=id)

        video_data = await video.info()
        media = await video.media()

        with open("out.json", "w") as out_file:
            json.dump(video_data, out_file)

        media.save("out")


if __name__ == "__main__":
    asyncio.run(main())
