"""Search TikTok for videos or users as a logged-in pooled account.

User search works anonymously, but video search requires a logged-in session:
TikTok's search/item endpoint returns empty responses for anonymous sessions.
Register + log in an account once (login_example.py or
`python -m pytok.accounts.cli login ...`), then this acquires it from the pool
already authenticated.
"""

import argparse
import asyncio
import json
import logging

from pytok.accounts import AccountsPool
from pytok.tiktok import PyTok


async def scrape_search(search_term, search_type, count, output, account_username, headless, media_dir=None):
    pool = AccountsPool()

    async with await PyTok.from_pool(pool, username=account_username, headless=headless) as api:
        search = api.search(search_term)
        if search_type == "user":
            source = search.users(count=count)
        else:
            source = search.videos(count=count, get_media=media_dir is not None)

        results = []
        async for result in source:
            results.append(await result.info())
            if getattr(result, "post_media", None) is not None:
                result.post_media.save(media_dir)

        with open(output, "w") as out_file:
            json.dump(results, out_file)

        logging.info("Saved %d %s results for '%s' to %s", len(results), search_type, search_term, output)


def main():
    parser = argparse.ArgumentParser(
        description="Search TikTok for videos or users as a pooled, logged-in account."
    )
    parser.add_argument("--term", default="therock", help="The phrase to search for.")
    parser.add_argument("--type", default="video", choices=["video", "user"],
                        help="Search for videos or users.")
    parser.add_argument("--count", type=int, default=100, help="Maximum number of results to fetch.")
    parser.add_argument("--output", default="out.json", help="Path to write the JSON results to.")
    parser.add_argument(
        "--account",
        default=None,
        help="Login identifier of the pool account to use. Omit to use the "
             "least-recently-used available account.",
    )
    parser.add_argument("--headless", action="store_true", help="Run the browser headless.")
    parser.add_argument("--media-dir", default=None,
                        help="With --type video, also download each post's media (MP4, or a photo post's images and sound) here.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    asyncio.run(scrape_search(
        args.term, args.type, args.count, args.output, args.account, args.headless, args.media_dir,
    ))


if __name__ == "__main__":
    main()
