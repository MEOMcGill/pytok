"""Fetch a video's comments as a pooled account.

Run login_example.py (or the accounts CLI) once first to register + log in an
account.
"""

import asyncio
import json

from pytok.tiktok import PyTok
from pytok.accounts import AccountsPool

videos = [
    {
        'id': '7058106162235100462',
        'author': {
            'uniqueId': 'charlesmcbryde'
        }
    }
]


async def main():
    pool = AccountsPool()

    async with await PyTok.from_pool(pool) as api:
        for video in videos:
            comments = []
            async for comment in api.video(id=video['id'], username=video['author']['uniqueId']).comments(count=1000):
                comments.append(comment)

            assert len(comments) > 0, "No comments found"
            print(f"Found {len(comments)} comments")
            with open("out.json", "w") as f:
                json.dump(comments, f)


if __name__ == "__main__":
    asyncio.run(main())
