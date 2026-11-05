
[![CI](https://github.com/MEOMcGill/pytok/actions/workflows/ci.yml/badge.svg)](https://github.com/MEOMcGill/pytok/actions/workflows/ci.yml)
[![License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![DOI](https://zenodo.org/badge/555492190.svg)](https://zenodo.org/doi/10.5281/zenodo.12802713)

# pytok

This is a [camoufox](https://github.com/daijro/camoufox) based version of David Teather's unofficial api wrapper for TikTok.com in python. It re-implements a set of features from the original library, with a shifted focus on using browser automation to allow automatic captcha solves with a hopefully minor trade-off in performance.

## Installation

```bash
pip install git+https://github.com/MEOMcGill/pytok.git@master
```

> [!WARNING]
> Install from this repository, not from PyPI. The `pytok` name on PyPI belongs to
> an unrelated project, so `pip install pytok` will get you different software.

PyTok drives camoufox, a Firefox build hardened against fingerprinting. Its browser is
downloaded on first launch, or ahead of time with:

```bash
python -m camoufox fetch
```

> [!NOTE]
> camoufox 0.5 deletes a camoufox 0.4 browser cache it finds on first run. If another
> project on the same machine still uses camoufox 0.4, give one of them its own cache
> location first.

## Quick Start Guide

Scrape as a logged-in account, acquired from an **accounts pool**. Anonymous sessions barely work any more — TikTok returns empty responses to most endpoints without a login — so this is the supported way to use PyTok. Register and log in an account **once** (see [Accounts, login, and persistent sessions](#accounts-login-and-persistent-sessions) below), then every run comes up already authenticated:

```bash
python -m pytok.accounts.cli add   --username you@email.com --password 'your-password'
python -m pytok.accounts.cli login --username you@email.com
```

Here's a quick bit of code to get the videos from a particular user on TikTok. There's more examples in the [examples](https://github.com/MEOMcGill/pytok/tree/master/examples) directory.

```py
import asyncio

from pytok.tiktok import PyTok
from pytok.accounts import AccountsPool

async def main():
    pool = AccountsPool()
    async with await PyTok.from_pool(pool) as api:
        user = api.user(username="therock")
        user_data = await user.info()
        print(user_data)

        videos = []
        async for video in user.videos():
            video_data = await video.info()
            print(video_data)

if __name__ == "__main__":
    asyncio.run(main())
```


The same pattern works for the other feeds — hashtags, search, and sounds:

```py
async for video in api.hashtag(name="funny").videos(count=100):
    ...
async for video in api.search("news").videos(count=100):
    ...
# Music id: the trailing number of a sound's URL
# (tiktok.com/music/original-sound-7016547803243022337), also in every
# video's `music` dict.
sound = api.sound(id="7016547803243022337")
print(await sound.info())
async for video in sound.videos(count=100):
    ...
```

To download media, call `video.media()`, or pass `get_media=True` to any of these listings to fetch it as each video is yielded. Either way you get a `PostMedia`: the MP4 of a video post, or the images and sound of a photo/slideshow post, which `save()` writes to a directory:

```py
async for video in api.user(username="therock").videos(count=100, get_media=True):
    if video.post_media is not None:  # None if that post's download failed
        video.post_media.save("media/")  # <id>.mp4, or <id>_01.jpg, ..., <id>.m4a
```

Please note pulling data from TikTok takes a while! We recommend leaving the scripts running on a server for a while for them to finish downloading everything. Feel free to play around with the delay constants to either speed up the process or avoid TikTok rate limiting, like so: `PyTok(request_delay=10)`

## Accounts, login, and persistent sessions

PyTok supports scraping as a logged-in account, and managing multiple accounts, via an **accounts pool**: a SQLite-backed set of TikTok accounts, each with its own persistent Firefox profile (and the browser fingerprint it was first given) and a cookie/identity backup. You register an account and log in **once** (interactively), and every session afterwards comes up already authenticated from that profile, repairing itself from the cookie backup if the profile's session is lost.

The pool lives in `~/.pytok` by default (override with the `$PYTOK_HOME` env var). The database holds credentials and cookie backups in plaintext, so keep that directory private — it is deliberately kept outside the repo.

Register an account and log in once with the CLI:

```bash
# Add the account (credentials are stored in ~/.pytok/accounts.db)
python -m pytok.accounts.cli add --username you@email.com --password 'your-password'

# Open a browser and log in. Complete any email/SMS/captcha verification in the
# window; on success PyTok captures the account identity and a cookie backup.
python -m pytok.accounts.cli login --username you@email.com

# Inspect the pool
python -m pytok.accounts.cli list -v
```

Then scrape as a logged-in account with `PyTok.from_pool`, which acquires an available account (or a specific one via `username=`) already signed in:

```py
import asyncio

from pytok.tiktok import PyTok
from pytok.accounts import AccountsPool

async def main():
    pool = AccountsPool()
    async with await PyTok.from_pool(pool) as api:
        hashtag = api.hashtag(name="fyp")
        async for video in hashtag.videos(count=100):
            print(await video.info())

if __name__ == "__main__":
    asyncio.run(main())
```

Other useful CLI commands: `info <username>`, `stats`, `activate`/`deactivate`, `release` (recover an account left in-use by a crashed run), `unlock`, and `delete`. Run `python -m pytok.accounts.cli --help` for the full list.

Each account's browser profile lives in `~/.pytok/firefox-profiles/<username>`, alongside a `pytok-fingerprint.json` holding the browser fingerprint that profile was first given. Every later session reuses it, so the account keeps appearing from the same device. `login --seed-profile <dir>` copies a warmed Firefox profile into a new account's profile dir first; the source's fingerprint file is not copied, so the new account gets its own.

### Upgrading from the zendriver (Chrome) version

Earlier versions kept a Chrome profile per account, which Firefox cannot open. The first time this version opens an accounts database, it forgets those profile dirs (leaving them on disk). Each account then starts a fresh Firefox profile and logs in from its stored cookie backup. If that backup has gone stale, log the account in again with `python -m pytok.accounts.cli login --username ...`. An older PyTok sharing the same database keeps working, since it falls back to its default Chrome profile location.

## Running headless

`PyTok(headless=True)` runs Firefox without a window, but TikTok treats true headless browsers with more suspicion. On Linux, prefer `headless="virtual"`, which runs a normal browser inside a virtual display (needs `Xvfb` installed).

## Followers and following

`user.followers()` and `user.following()` page through a profile's follower and following
lists, yielding a `User` for each account with its profile fields and stats in `as_dict`.
Followers come most recent first. Both lists are read through `api/user/list`, the same
request the profile's own follower panel makes.

```py
async with await PyTok.from_pool(pool) as api:
    user = api.user(username="therock")
    async for follower in user.followers(count=500):
        print(follower.username, follower.as_dict["followerCount"])
    following = [u.username async for u in user.following()]
```

## The For You feed

`api.for_you().videos()` reads the home feed of the logged-in pool account, so the videos
are that account's personalised recommendations, and a second read gives different ones.
By default it replays the page's own recommend request for each batch; with
`prefer_scraping=True` it moves through the feed in the page instead, so every video is
actually shown to the account.

```py
async with await PyTok.from_pool(pool) as api:
    async for video in api.for_you().videos(count=50):
        print(video.as_dict["author"]["uniqueId"], video.id)
```

From a phone, `PhoneTikTok.for_you()` swipes through mobile web's feed (see below).
Mobile web stops moving the feed after its first batch, so it returns about five videos
logged out and eight or nine from a phone logged into its bound pool account.

Phones are bound to pool accounts with `python -m pytok.accounts.cli bind-phone SERIAL
USERNAME`; `PhoneTikTok.from_pool(pool, serial)` then holds that account for the session
and checks the phone's Chrome is logged into it. `PhoneTikTok.import_cookies()` logs a
phone in from the account's saved cookies, and `login()` with its password.

## Scraping concurrently across accounts

`WorkerPool` runs many sessions at once — each worker owns one account and its own isolated Firefox profile, so N accounts means N concurrent scrapers. Tasks are plain async callables `async def task(api) -> result` distributed across a shared queue:

```py
import asyncio

from pytok.accounts import AccountsPool, WorkerPool

async def scrape_user(api, handle):
    videos = []
    async for video in api.user(username=handle).videos(count=100):
        videos.append(await video.info())
    return handle, videos

async def main():
    pool = AccountsPool()
    async with WorkerPool(pool, max_workers=3) as wp:
        results = await wp.run([
            lambda api, h=h: scrape_user(api, h)
            for h in ["therock", "khaby.lame", "charlidamelio"]
        ])
    for handle, videos in results:
        print(f"@{handle}: {len(videos)} videos")

if __name__ == "__main__":
    asyncio.run(main())
```

`max_workers` is capped to the number of active accounts. Workers rotate/rest accounts and rebuild crashed sessions automatically.

Before launching an account's browser, a worker kills any browser still running on that account's profile, since one left behind by an abandoned teardown keeps the profile locked. If the browser still won't start, the error (`BrowserLaunchException`) names the profile's lock files, any processes on it and the free disk space, and the worker rotates to another account. If the browser fails to launch on every active account with no success in between, the host is at fault. The pool then fails every remaining task with `HostBrowserLaunchException` rather than cycling through the accounts again. See [`examples/worker_pool_example.py`](https://github.com/MEOMcGill/pytok/tree/master/examples/worker_pool_example.py).

Please do not hesitate to make an issue in this repo to get our help with this!

## Scraping from a physical Android phone

`pytok.phone` is a third data route, alongside the API requests and the desktop browser:
it drives **Chrome on a real handset** over `adb` + the Chrome DevTools Protocol, reading
TikTok's mobile web. The phone presents a genuine mobile fingerprint that no desktop
stealth patch reproduces, and in testing an anonymous phone session returned a full
listing with no captcha and no login.

```py
import asyncio
from pytok.phone import PhoneTikTok, list_serials

async def main():
    serials = await list_serials()          # phones in adb state "device"
    async with PhoneTikTok(serials[0]) as phone:
        user = await phone.user_info("therock")
        videos = await phone.user_videos("therock")
        feed = await phone.for_you()           # first For You batch
        print(user["followerCount"], len(videos), len(feed))

asyncio.run(main())
```

`user_info()` returns the same dict shape as `User.info()`, and `user_videos()` returns raw
`itemList` dicts that go straight into `utils.get_video_df` — so phone-sourced rows join
cleanly with rows from the other routes.

Phones can live on a remote host (one SSH hop) and are configured by argument or env var:

```bash
PYTOK_PHONE_SSH=meo-laptop PYTOK_ADB='C:\platform-tools\adb.exe' \
    python examples/phone_example.py therock
```

Drive several at once by giving each its own `cdp_port`.

**Two limits worth knowing before you plan a crawl around this.** Phones on one WiFi share
a single egress IP and TikTok's burst limits are largely IP-level, so N handsets do *not*
give N times the rate-limit headroom — only SIMs or proxies would. And mobile web serves
one `item_list` page (~35 videos) per profile, with no infinite scroll to drive.
`user_videos()` logs when it hits that ceiling rather than passing a truncated listing off
as a complete one. Use this route for breadth across many profiles, not depth on one.

Fetching the next page yourself from inside the page does not work, and the module
docstring records the measurements in full so nobody re-derives them: TikTok's SDK really
does re-sign an injected `fetch` (`X-Bogus`/`X-Gnarly` appear on the wire), the request
headers are identical to the app's own, and `msToken` is not a single-use nonce — yet the
app's request returns ~82 KB and an identical injected one returns an empty 200. Depth
needs a logged-in session, not better request forgery.

## Citation

If you use this library in your research, please cite it using the following BibTeX entry:

```bibtex
@article{steel2023invasion,
  title={The invasion of ukraine viewed through tiktok: A dataset},
  author={Steel, Benjamin and Parker, Sara and Ruths, Derek},
  journal={arXiv preprint arXiv:2301.08305},
  year={2023}
}

```

## Format and Schema

The JSONable dictionary returned by the `info()` methods contains all of the data that the TikTok API returns. We have provided helper functions to parse that data into Pandas DataFrames, `utils.get_comment_df()`, `utils.get_video_df()` and `utils.get_user_df()` for the data from comments, videos, and users respectively.

The video dataframe will contain the following columns:
|Field name | Description |
|----------|----------|
|`video_id`| Unique video ID |
|`createtime`| UTC datetime of video creation time in YYYY-MM-DD HH:MM:SS format |
|`author_name`| Unique author name |
|`author_id`| Unique author ID |
|`desc`| The full video description from the author |
|`hashtags`| A list of hashtags used in the video description |
|`share_video_id`| If the video is sharing another video, this is the video ID of that original video, else empty |
|`share_video_user_id`| If the video is sharing another video, this the user ID of the author of that video, else empty |
|`share_video_user_name`| If the video is sharing another video, this is the user name of the author of that video, else empty |
|`share_type`| If the video is sharing another video, this is the type of the share, stitch, duet etc. |
|`mentions`| A list of users mentioned in the video description, if any |
|`digg_count`| The number of likes on the video |
|`share_count`| The number of times the video was shared |
|`comment_count`| The number of comments on the video |
|`play_count`| The number of times the video was played |

The comment dataframe will contain the following columns:
|Field name | Description |
|----------|-----------|
|`comment_id`| Unique comment ID |
|`createtime`| UTC datetime of comment creation time in YYYY-MM-DD HH:MM:SS format |
|`author_name`| Unique author name |
|`author_id`| Unique author ID |
|`text`| Text of the comment |
|`mentions`| A list of users that are tagged in the comment |
|`video_id`| The ID of the video the comment is on |
|`comment_language`| The language of the comment, as predicted by the TikTok API |
|`digg_count`| The number of likes the comment got |
|`reply_comment_id`| If the comment is replying to another comment, this is the ID of that comment |

The user dataframe will contain the following columns:
|Field name | Description |
|----------|-----------|
|`id`| Unique author ID |
|`unique_id`| Unique user name |
|`nickname`| Display user name, changeable |
|`signature`| Short user description |
|`verified`| Whether or not the user is verified |
|`num_following`| How many other accounts the user is following |
|`num_followers`| How many followers the user has |
|`num_videos`| How many videos the user has made |
|`num_likes`| How many total likes the user has had |
|`createtime`| When the user account was made. This is derived from the `id` field, and can occasionally be incorrect with a very low unix epoch such as 1971 |

