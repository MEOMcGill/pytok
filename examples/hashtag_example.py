"""Scrape videos for a TikTok hashtag as a logged-in pooled account.

Hashtag video listing requires a logged-in session: TikTok's challenge/item_list
endpoint returns empty responses for anonymous sessions. Register + log in an
account once (login_example.py or `python -m pytok.accounts.cli login ...`), then
this acquires it from the pool already authenticated.
"""

import argparse
import asyncio
import json
import logging

from pytok.accounts import AccountsPool
from pytok.tiktok import PyTok


async def scrape_hashtag(hashtag_name, count, output, account_username, headless, media_dir=None):
    pool = AccountsPool()

    # from_pool acquires the given account (or the least-recently-used available
    # one) already logged in from its persistent profile.
    async with await PyTok.from_pool(pool, username=account_username, headless=headless) as api:
        hashtag = api.hashtag(name=hashtag_name)

        videos = []
        async for video in hashtag.videos(count=count, get_media=media_dir is not None):
            video_info = await video.info()
            videos.append(video_info)
            if video.post_media is not None:
                video.post_media.save(media_dir)

        with open(output, "w") as out_file:
            json.dump(videos, out_file)

        logging.info("Saved %d videos for #%s to %s", len(videos), hashtag_name, output)


def main():
    parser = argparse.ArgumentParser(
        description="Scrape videos for a TikTok hashtag as a pooled, logged-in account."
    )
    parser.add_argument("--hashtag", default="fyp", help="Hashtag name, without the leading '#'.")
    parser.add_argument("--count", type=int, default=100, help="Maximum number of videos to fetch.")
    parser.add_argument("--output", default="out.json", help="Path to write the JSON results to.")
    parser.add_argument(
        "--account",
        default=None,
        help="Login identifier of the pool account to use. Omit to use the "
             "least-recently-used available account.",
    )
    parser.add_argument("--headless", action="store_true", help="Run the browser headless.")
    parser.add_argument("--media-dir", default=None,
                        help="Also download each post's media (MP4, or a photo post's images and sound) here.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    asyncio.run(scrape_hashtag(
        args.hashtag, args.count, args.output, args.account, args.headless, args.media_dir,
    ))


if __name__ == "__main__":
    main()
