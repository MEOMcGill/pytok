"""Scrape TikTok mobile web from a physical Android phone over adb + CDP.

Unlike the other examples this one needs no accounts pool: the phone's own mobile Chrome
session gets profile data anonymously. It does need a handset with USB debugging
authorized -- see `pytok/phone.py` for the prerequisites and the limits (first item_list
page only, and no IP diversity between phones on one WiFi).

    # phones on a remote farm laptop:
    PYTOK_PHONE_SSH=meo-laptop PYTOK_ADB='C:\\platform-tools\\adb.exe' \
        python examples/phone_example.py therock

    # a phone plugged into this machine:
    python examples/phone_example.py therock
"""

import asyncio
import logging
import sys

from pytok.phone import PhoneTikTok, list_serials
from pytok.utils import get_video_df

logging.basicConfig(level=logging.INFO)


async def main(username: str) -> None:
    serials = await list_serials()
    if not serials:
        raise SystemExit("no drivable phones -- check `adb devices` and USB debugging")
    print(f"phones available: {serials}")

    async with PhoneTikTok(serials[0]) as phone:
        user = await phone.user_info(username)
        print(f"\n@{user['uniqueId']} -- {user.get('nickname')}")
        print(f"  followers: {user.get('followerCount'):,}")
        print(f"  videos:    {user.get('videoCount'):,}")

        videos = await phone.user_videos(username)
        print(f"\ngot {len(videos)} videos")
        for video in videos[:5]:
            stats = video.get("stats", {})
            print(f"  {video['id']}  plays={stats.get('playCount'):>10,}  "
                  f"{(video.get('desc') or '')[:50]}")

        if videos:
            # The raw itemList dicts drop straight into pytok's dataframe helpers.
            print(f"\ndataframe: {get_video_df(videos).shape}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "therock"))
