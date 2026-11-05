"""Collect the followers and following of TikTok users as a parquet of edges."""

import argparse
import asyncio
import logging

import polars as pl

from pytok.accounts import AccountsPool
from pytok.tiktok import PyTok


async def collect(usernames, count):
    rows = []
    async with await PyTok.from_pool(AccountsPool()) as api:
        for username in usernames:
            user = api.user(username=username)
            for relation, listing in (("follower", user.followers), ("following", user.following)):
                async for other in listing(count=count):
                    rows.append({
                        "user": username,
                        "relation": relation,
                        "other_id": other.user_id,
                        "other_username": other.username,
                        "other_follower_count": other.as_dict.get("followerCount"),
                    })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("usernames", nargs="+")
    parser.add_argument("--count", type=int, default=1000, help="max accounts per list")
    parser.add_argument("--output", default="follow_lists.parquet")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    rows = asyncio.run(collect(args.usernames, args.count))
    pl.DataFrame(rows).write_parquet(args.output, compression="zstd")
    logging.info(f"Wrote {len(rows)} edges to {args.output}")


if __name__ == "__main__":
    main()
