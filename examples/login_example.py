"""Register an account in the pool and log it in once.

The new multi-account pattern replaces per-script ``api.login(...)`` calls: you
register each account in a SQLite-backed pool once, log in interactively a single
time, and thereafter every session comes up already authenticated from the
account's persistent Chrome profile (repairing from the cookie backup if needed).

This is the programmatic equivalent of the CLI:

    python -m pytok.accounts.cli add   --username you@email.com --password ...
    python -m pytok.accounts.cli login --username you@email.com

After this runs once, the other examples can scrape as this account via
``PyTok.from_pool(pool)`` with no further login.
"""

import asyncio
import logging
import os

from dotenv import load_dotenv

from pytok.tiktok import PyTok
from pytok.accounts import AccountsPool

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


async def main():
    username = os.environ.get('TIKTOK_USERNAME')
    password = os.environ.get('TIKTOK_PASSWORD')

    if not username:
        print("Set TIKTOK_USERNAME (and optionally TIKTOK_PASSWORD) to log in.")
        print("Example: TIKTOK_USERNAME=you@email.com TIKTOK_PASSWORD=pass python login_example.py")
        return

    pool = AccountsPool()  # ~/.pytok/accounts.db by default

    # Register the account if it isn't in the pool yet. add_account is idempotent
    # in intent — guard so re-running doesn't error on a duplicate.
    if await pool.get(username) is None:
        await pool.add_account(username=username, password=password)
        print(f"Added {username} to the pool")

    # Acquire it and open a session. __aenter__ runs the login/verification flow:
    # it fills the stored credentials, and you complete any email/SMS/captcha
    # verification manually in the browser window. On success it captures the
    # TikTok identity and snapshots cookies to the pool.
    account = await pool.get_account(username)
    if account is None:
        print(f"{username} is already in use by another session — release it first "
              f"(python -m pytok.accounts.cli release {username}).")
        return
    try:
        async with PyTok(account=account, accounts_pool=pool, logging_level=logging.INFO) as api:
            ident = await api._get_logged_in_identity()
            if ident:
                print(f"Logged in as @{ident.get('unique_id')} (uid {ident.get('user_id')})")
            else:
                print("Session opened but identity could not be read.")
    finally:
        await pool.release_account(username)


if __name__ == "__main__":
    asyncio.run(main())
