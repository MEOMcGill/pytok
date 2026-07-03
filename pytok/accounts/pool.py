"""AccountsPool — CRUD + concurrency-safe acquisition for TikTok accounts.

Ported from igscrape's AccountsPool (twscrape lineage), keyed on the login
identifier `username`. `get_available()` atomically picks the least-recently-used
active, unlocked, free account and marks it in_use so concurrent workers never
grab the same one.
"""

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime

from .account import Account
from .db import execute, fetchall, fetchone
from ._utils import default_db_path, get_env_bool, parse_cookies, utc

logger = logging.getLogger("PyTok")


class NoAccountError(Exception):
    """No account available in the pool."""


class AccountsPool:
    _order_by: str = "scrape_count_overall_24h ASC, last_used ASC"

    def __init__(
        self,
        db_file: str | None = None,
        raise_when_no_account: bool = False,
    ):
        self._db_file = db_file or default_db_path()
        self._raise_when_no_account = raise_when_no_account or get_env_bool(
            "PYTOK_RAISE_WHEN_NO_ACCOUNT"
        )

    @staticmethod
    def _id_cond(username: str) -> str:
        return f"username = '{username}'"

    @staticmethod
    def _ids_cond(usernames: list[str]) -> str:
        quoted = ",".join([f"'{x}'" for x in usernames])
        return f"username IN ({quoted})"

    # ==================== CRUD ====================

    async def add_account(
        self,
        username: str,
        password: str | None = None,
        email: str | None = None,
        email_password: str | None = None,
        phone_number: str | None = None,
        cookies=None,
        profile_dir: str | None = None,
        proxy_server: str | None = None,
        proxy_username: str | None = None,
        proxy_password: str | None = None,
    ):
        """Add an account, keyed on the login identifier `username`."""
        if not username:
            raise ValueError("Must provide username")

        qs = f"SELECT * FROM accounts WHERE {self._id_cond(username)}"
        if await fetchone(self._db_file, qs):
            logger.warning(f"Account {username} already exists")
            return

        cookies = parse_cookies(cookies)
        account = Account(
            username=username,
            password=password,
            email=email,
            email_password=email_password,
            phone_number=phone_number,
            cookies=cookies,
            active=bool(cookies),
            profile_dir=profile_dir,
            proxy_server=proxy_server,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
        )
        await self.save(account)
        logger.info(f"Account {username} added (active={account.active})")

    async def delete_account(self, username):
        usernames = username if isinstance(username, list) else [username]
        usernames = list(set(usernames))
        if not usernames:
            return
        await execute(
            self._db_file, f"DELETE FROM accounts WHERE {self._ids_cond(usernames)}"
        )
        logger.info(f"Deleted {len(usernames)} account(s)")

    async def get(self, username):
        if username is None:
            rs = await fetchall(self._db_file, "SELECT * FROM accounts")
            return [Account.from_rs(x) for x in rs]
        elif isinstance(username, list):
            usernames = list(set(username))
            qs = f"SELECT * FROM accounts WHERE {self._ids_cond(usernames)}"
            rs = await fetchall(self._db_file, qs)
            return [Account.from_rs(x) for x in rs]
        else:
            qs = f"SELECT * FROM accounts WHERE {self._id_cond(username)}"
            rs = await fetchone(self._db_file, qs)
            if not rs:
                raise ValueError(f"Account {username} not found")
            return Account.from_rs(rs)

    async def get_active_accounts(self) -> list[Account]:
        rs = await fetchall(self._db_file, "SELECT * FROM accounts WHERE active = true")
        return [Account.from_rs(x) for x in rs]

    async def get_inactive_accounts(self) -> list[Account]:
        rs = await fetchall(self._db_file, "SELECT * FROM accounts WHERE active = false")
        return [Account.from_rs(x) for x in rs]

    async def save(self, account: Account):
        data = account.to_rs()
        cols = list(data.keys())
        username = account.username

        existing = await fetchone(
            self._db_file, f"SELECT * FROM accounts WHERE {self._id_cond(username)}"
        )
        if existing:
            set_clause = ",".join([f"{x}=:{x}" for x in cols if x != "username"])
            qs = f"UPDATE accounts SET {set_clause} WHERE {self._id_cond(username)}"
        else:
            qs = (
                f"INSERT INTO accounts ({','.join(cols)}) "
                f"VALUES ({','.join([f':{x}' for x in cols])})"
            )
        await execute(self._db_file, qs, data)

    # ==================== Identity & cookies ====================

    async def set_identity(
        self,
        username: str,
        user_id: str | None = None,
        sec_uid: str | None = None,
        unique_id: str | None = None,
    ):
        """Persist the resolved on-platform identity captured at login."""
        qs = (
            f"UPDATE accounts SET user_id = :user_id, sec_uid = :sec_uid, "
            f"unique_id = :unique_id WHERE {self._id_cond(username)}"
        )
        await execute(
            self._db_file,
            qs,
            {"user_id": user_id, "sec_uid": sec_uid, "unique_id": unique_id},
        )
        logger.info(f"Set identity for {username}: uid={user_id} handle={unique_id}")

    async def update_cookies(self, username: str, cookies):
        cookies = parse_cookies(cookies)
        qs = f"UPDATE accounts SET cookies = :cookies WHERE {self._id_cond(username)}"
        await execute(self._db_file, qs, {"cookies": json.dumps(cookies)})
        logger.debug(f"Updated cookie backup for {username} ({len(cookies)} cookies)")

    # ==================== Locking / activation ====================

    async def set_active(self, username, active: bool, error_message: str | None = None):
        if username is None:
            qs = "UPDATE accounts SET active = :active, error_msg = :error_msg"
            await execute(self._db_file, qs, {"active": active, "error_msg": error_message})
        else:
            usernames = username if isinstance(username, list) else [username]
            qs = (
                f"UPDATE accounts SET active = :active, error_msg = :error_msg "
                f"WHERE {self._ids_cond(list(set(usernames)))}"
            )
            await execute(self._db_file, qs, {"active": active, "error_msg": error_message})
        logger.info(f"Set active={active} for {username if username else 'all accounts'}")

    async def mark_inactive(self, username: str, error_msg: str | None):
        qs = (
            f"UPDATE accounts SET active = false, error_msg = :error_msg, in_use = false "
            f"WHERE {self._id_cond(username)}"
        )
        await execute(self._db_file, qs, {"error_msg": error_msg})
        logger.warning(f"Marked account {username} inactive: {error_msg}")

    async def lock_until(self, username, until: str):
        """Lock account(s) until a SQLite datetime expr, e.g. "datetime('now', '+15 minutes')"."""
        usernames = username if isinstance(username, list) else [username] if username else []
        where = self._ids_cond(list(set(usernames))) if usernames else "TRUE"
        qs = f"""
        UPDATE accounts SET
            locks = json_set(locks, '$.locked_until', {until}),
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE {where}
        """
        await execute(self._db_file, qs)

    async def unlock(self, username):
        usernames = username if isinstance(username, list) else [username] if username else []
        where = self._ids_cond(list(set(usernames))) if usernames else "TRUE"
        qs = f"""
        UPDATE accounts SET
            locks = json_remove(locks, '$.locked_until'),
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE {where}
        """
        await execute(self._db_file, qs)

    async def reset_locks(self, username=None):
        if username is None:
            qs = "UPDATE accounts SET locks = json_object()"
        else:
            usernames = username if isinstance(username, list) else [username]
            qs = f"UPDATE accounts SET locks = json_object() WHERE {self._ids_cond(list(set(usernames)))}"
        await execute(self._db_file, qs)

    # ==================== Acquisition ====================

    async def _get_and_mark_in_use(self, subquery: str) -> Account | None:
        if int(sqlite3.sqlite_version_info[1]) >= 35:
            qs = f"""
            UPDATE accounts SET
                last_used = datetime({utc.ts()}, 'unixepoch'),
                in_use = true
            WHERE username = ({subquery})
            RETURNING *
            """
            rs = await fetchone(self._db_file, qs)
        else:
            tx = uuid.uuid4().hex
            qs = f"""
            UPDATE accounts SET
                last_used = datetime({utc.ts()}, 'unixepoch'),
                in_use = true,
                _tx = '{tx}'
            WHERE username = ({subquery})
            """
            await execute(self._db_file, qs)
            rs = await fetchone(self._db_file, f"SELECT * FROM accounts WHERE _tx = '{tx}'")
        return Account.from_rs(rs) if rs else None

    async def get_available(self) -> Account | None:
        q = f"""
        SELECT username FROM accounts
        WHERE active = true
          AND in_use = false
          AND (
                locks IS NULL
                OR json_extract(locks, '$.locked_until') IS NULL
                OR json_extract(locks, '$.locked_until') < datetime('now')
          )
        ORDER BY {self._order_by}
        LIMIT 1
        """
        return await self._get_and_mark_in_use(q)

    async def get_account(self, username: str) -> Account | None:
        """Acquire a specific account by login identifier, marking it in_use.

        Unlike get_available() this ignores the ordering and locks, but still
        refuses an account already in_use so two sessions can't share it.
        """
        q = f"SELECT username FROM accounts WHERE {self._id_cond(username)} AND in_use = false"
        return await self._get_and_mark_in_use(q)

    async def get_available_or_wait(self, poll: float = 5.0) -> Account | None:
        msg_shown = False
        while True:
            account = await self.get_available()
            if account:
                if msg_shown:
                    logger.info(f"Continuing with account {account.username}")
                return account

            if self._raise_when_no_account:
                raise NoAccountError("No account available")

            if not msg_shown:
                nat = await self.next_available_at()
                if not nat:
                    logger.warning("No active accounts. Stopping...")
                    return None
                logger.info(f"No account available. Next available at {nat}")
                msg_shown = True

            await asyncio.sleep(poll)

    async def next_available_at(self):
        qs = """
        SELECT json_extract(locks, '$.locked_until') AS locked_until
        FROM accounts
        WHERE active = true
          AND json_extract(locks, '$.locked_until') IS NOT NULL
          AND json_extract(locks, '$.locked_until') > datetime('now')
        ORDER BY locked_until ASC
        LIMIT 1
        """
        rs = await fetchone(self._db_file, qs)
        if rs and rs["locked_until"]:
            now, trg = utc.now(), utc.from_iso(rs["locked_until"])
            if trg < now:
                return "now"
            at_local = datetime.now() + (trg - now)
            return at_local.strftime("%H:%M:%S")
        return None

    async def release_account(self, username):
        usernames = username if isinstance(username, list) else [username] if username else []
        where = self._ids_cond(list(set(usernames))) if usernames else "TRUE"
        qs = f"""
        UPDATE accounts SET
            in_use = false,
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE {where}
        """
        await execute(self._db_file, qs)

    async def update_last_used(self, username: str):
        qs = (
            f"UPDATE accounts SET last_used = datetime({utc.ts()}, 'unixepoch') "
            f"WHERE {self._id_cond(username)}"
        )
        await execute(self._db_file, qs)

    # ==================== Counters ====================

    async def increment_scrape_count(self, username: str, increment: int = 1):
        qs = f"""
        UPDATE accounts SET
            scrape_count_since_rest = scrape_count_since_rest + :inc,
            scrape_count_overall_24h = scrape_count_overall_24h + :inc,
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE {self._id_cond(username)}
        """
        await execute(self._db_file, qs, {"inc": increment})

    async def reset_scrape_counts(self, username=None):
        base = "UPDATE accounts SET scrape_count_since_rest = 0, scrape_count_overall_24h = 0"
        qs = base if username is None else f"{base} WHERE {self._id_cond(username)}"
        await execute(self._db_file, qs)

    # ==================== Stats ====================

    async def stats(self) -> dict:
        config = [
            ("total", "SELECT COUNT(*) FROM accounts"),
            ("active", "SELECT COUNT(*) FROM accounts WHERE active = true"),
            ("inactive", "SELECT COUNT(*) FROM accounts WHERE active = false"),
            ("in_use", "SELECT COUNT(*) FROM accounts WHERE in_use = true"),
            (
                "locked",
                "SELECT COUNT(*) FROM accounts "
                "WHERE json_extract(locks, '$.locked_until') IS NOT NULL "
                "AND json_extract(locks, '$.locked_until') > datetime('now')",
            ),
        ]
        qs = f"SELECT {','.join([f'({q}) as {k}' for k, q in config])}"
        rs = await fetchone(self._db_file, qs)
        return dict(rs) if rs else {}
