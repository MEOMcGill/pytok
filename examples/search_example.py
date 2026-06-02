import argparse
import asyncio
import json
import logging
import os

from pytok.tiktok import PyTok


async def scrape_search(search_term, search_type, count, output, chrome_profile, username, password, headless):
    pytok_kwargs = {"headless": headless}
    if chrome_profile:
        # A Chrome user-data dir that is already signed in to TikTok. Reusing it
        # avoids logging in every run and persists the session across runs.
        pytok_kwargs["user_data_dir"] = os.path.expanduser(chrome_profile)

    async with PyTok(**pytok_kwargs) as api:
        # User search works without logging in, but video search requires a
        # logged-in session: TikTok's search/item endpoint returns empty
        # responses for anonymous sessions, and the web search feed is
        # login-walled.
        if chrome_profile:
            # The profile already carries a logged-in session; with no
            # credentials login() just verifies and refreshes the API tokens.
            await api.login()
        elif username and password:
            await api.login(username=username, password=password)
        elif search_type == "video":
            logging.warning(
                "No --chrome-profile or credentials supplied. TikTok requires "
                "login to search videos, so this will likely return nothing."
            )

        search = api.search(search_term)
        source = search.users(count=count) if search_type == "user" else search.videos(count=count)

        results = []
        async for result in source:
            results.append(await result.info())

        with open(output, "w") as out_file:
            json.dump(results, out_file)

        logging.info("Saved %d %s results for '%s' to %s", len(results), search_type, search_term, output)


def main():
    parser = argparse.ArgumentParser(
        description="Search TikTok for videos or users (video search requires a logged-in session)."
    )
    parser.add_argument("--term", default="therock", help="The phrase to search for.")
    parser.add_argument("--type", default="video", choices=["video", "user"],
                        help="Search for videos or users.")
    parser.add_argument("--count", type=int, default=100, help="Maximum number of results to fetch.")
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

    asyncio.run(scrape_search(
        args.term, args.type, args.count, args.output,
        args.chrome_profile, args.username, args.password, args.headless,
    ))


if __name__ == "__main__":
    main()
