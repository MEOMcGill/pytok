"""Scrape videos made with a TikTok sound (music id) as a logged-in pooled account.

Sound video listing requires a logged-in session: TikTok's music/item_list
endpoint returns empty responses for anonymous sessions. Register + log in an
account once (login_example.py or `python -m pytok.accounts.cli login ...`), then
this acquires it from the pool already authenticated.

The music id is the trailing number of a sound's URL
(https://www.tiktok.com/music/original-sound-7016547803243022337), and is also
in the `music` dict of every video's info.
"""

import argparse
import asyncio
import json
import logging

from pytok.accounts import AccountsPool
from pytok.tiktok import PyTok


async def scrape_sound(sound_id, count, output, account_username, headless):
    pool = AccountsPool()

    # from_pool acquires the given account (or the least-recently-used available
    # one) already logged in from its persistent profile.
    async with await PyTok.from_pool(pool, username=account_username, headless=headless) as api:
        sound = api.sound(id=sound_id)

        videos = []
        async for video in sound.videos(count=count):
            video_info = await video.info()
            videos.append(video_info)

        with open(output, "w") as out_file:
            json.dump(videos, out_file)

        logging.info(
            "Saved %d videos for sound %s (%s) to %s",
            len(videos), sound_id, sound.title, output,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Scrape videos made with a TikTok sound as a pooled, logged-in account."
    )
    parser.add_argument("--sound", required=True, help="TikTok music id, e.g. 7016547803243022337.")
    parser.add_argument("--count", type=int, default=100, help="Maximum number of videos to fetch.")
    parser.add_argument("--output", default="out.json", help="Path to write the JSON results to.")
    parser.add_argument(
        "--account",
        default=None,
        help="Login identifier of the pool account to use. Omit to use the "
             "least-recently-used available account.",
    )
    parser.add_argument("--headless", action="store_true", help="Run the browser headless.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    asyncio.run(scrape_sound(
        args.sound, args.count, args.output, args.account, args.headless,
    ))


if __name__ == "__main__":
    main()
