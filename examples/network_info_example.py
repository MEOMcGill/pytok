"""Inspect the network requests behind a video fetch, as a pooled account.

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
        network_data = await video.network_info()
        bytes_network_data = await video.bytes_network_info()

        all_data = {
            "video_data": video_data,
            "network_data": network_data,
            "bytes_network_data": bytes_network_data
        }

        with open("out.json", "w") as out_file:
            json.dump(all_data, out_file)


if __name__ == "__main__":
    asyncio.run(main())
