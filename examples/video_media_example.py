"""Download posts' media as a pooled account: the MP4 of a video post, and the
images and sound of a photo/slideshow post.

Run login_example.py (or the accounts CLI) once first to register + log in an
account.
"""

import asyncio
import json
import os

from pytok.accounts import AccountsPool
from pytok.tiktok import PyTok

posts = [
    ('tiktok', '7702947070604332319'),   # a video post
    ('tiktok', '7655092881387998494'),   # a photo post with a sound
]


async def main():
    pool = AccountsPool()

    os.makedirs("out", exist_ok=True)
    async with await PyTok.from_pool(pool) as api:
        for username, id in posts:
            video = api.video(username=username, id=id)
            video_data = await video.info()
            with open(f"out/{id}.json", "w") as out_file:
                json.dump(video_data, out_file)

            # <id>.mp4 for a video post; <id>_01.jpg, <id>_02.jpg, ... and <id>.m4a for a photo post
            media = await video.media()
            paths = media.save("out")
            kind = "photo post" if media.is_photo_post else "video post"
            print(f"{id} ({kind}): saved {', '.join(p.name for p in paths)}")


if __name__ == "__main__":
    asyncio.run(main())
