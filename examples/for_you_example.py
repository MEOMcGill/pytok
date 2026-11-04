"""Collect videos from the For You (home) feed, from a desktop browser or a phone.

Desktop reads the feed of a logged-in pooled account, so what comes back is that
account's personalised recommendations. The phone route reads mobile web on a real
Android handset, which stops after its first batch of five to nine videos.
"""

import argparse
import asyncio
import json
import logging

from pytok.accounts import AccountsPool
from pytok.phone import PhoneTikTok
from pytok.tiktok import PyTok


async def desktop(count, account_username, prefer_scraping):
    async with await PyTok.from_pool(AccountsPool(), username=account_username) as api:
        return [video.as_dict async for video in
                api.for_you().videos(count=count, prefer_scraping=prefer_scraping)]


async def phone(count, serial):
    # PYTOK_PHONE_SSH / PYTOK_ADB say where the phone is attached
    async with PhoneTikTok(serial) as handset:
        return await handset.for_you(count=count)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--output", default="for_you.json")
    parser.add_argument("--account", help="pool account (default: least recently used)")
    parser.add_argument("--prefer-scraping", action="store_true",
                        help="desktop: move through the feed in the page instead of replaying its request")
    parser.add_argument("--phone", metavar="SERIAL", help="read the feed from this adb serial instead")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.phone:
        videos = asyncio.run(phone(args.count, args.phone))
    else:
        videos = asyncio.run(desktop(args.count, args.account, args.prefer_scraping))
    with open(args.output, "w") as out_file:
        json.dump(videos, out_file)
    logging.info(f"Wrote {len(videos)} For You videos to {args.output}")


if __name__ == "__main__":
    main()
