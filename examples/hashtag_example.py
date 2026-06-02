import argparse
import asyncio
import json
import logging
import os

from pytok.tiktok import PyTok


async def scrape_hashtag(hashtag_name, count, output, chrome_profile, username, password, headless):
    pytok_kwargs = {"headless": headless}
    if chrome_profile:
        # A Chrome user-data dir that is already signed in to TikTok. Reusing it
        # avoids logging in every run and persists the session across runs.
        pytok_kwargs["user_data_dir"] = os.path.expanduser(chrome_profile)

    async with PyTok(**pytok_kwargs) as api:
        # Hashtag video listing requires a logged-in session: TikTok's
        # challenge/item_list endpoint returns empty responses for anonymous
        # sessions, and the web hashtag feed is login-walled.
        if chrome_profile:
            # The profile already carries a logged-in session; with no
            # credentials login() just verifies and refreshes the API tokens.
            await api.login()
        elif username and password:
            await api.login(username=username, password=password)
        else:
            logging.warning(
                "No --chrome-profile or credentials supplied. TikTok requires "
                "login to list hashtag videos, so this will likely return nothing."
            )

        hashtag = api.hashtag(name=hashtag_name)

        videos = []
        async for video in hashtag.videos(count=count):
            video_info = await video.info()
            videos.append(video_info)

        with open(output, "w") as out_file:
            json.dump(videos, out_file)

        logging.info("Saved %d videos for #%s to %s", len(videos), hashtag_name, output)


def main():
    parser = argparse.ArgumentParser(
        description="Scrape videos for a TikTok hashtag (requires a logged-in session)."
    )
    parser.add_argument("--hashtag", default="fyp", help="Hashtag name, without the leading '#'.")
    parser.add_argument("--count", type=int, default=100, help="Maximum number of videos to fetch.")
    parser.add_argument("--output", default="out.json", help="Path to write the JSON results to.")
    parser.add_argument(
        "--chrome-profile",
        default=os.environ.get("TIKTOK_CHROME_PROFILE"),
        help="Path to a Chrome user-data dir already signed in to TikTok "
             "(or set the TIKTOK_CHROME_PROFILE env var). Pass it at runtime so "
             "your profile path never ends up in source control.",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("TIKTOK_USERNAME"),
        help="TikTok username/email for an automatic login (or set TIKTOK_USERNAME). "
             "Used only when --chrome-profile is not given.",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("TIKTOK_PASSWORD"),
        help="TikTok password (or set TIKTOK_PASSWORD). Used only when "
             "--chrome-profile is not given.",
    )
    parser.add_argument("--headless", action="store_true", help="Run the browser headless.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    asyncio.run(scrape_hashtag(
        args.hashtag, args.count, args.output,
        args.chrome_profile, args.username, args.password, args.headless,
    ))


if __name__ == "__main__":
    main()
