"""TikTok account record for the accounts pool."""

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime

from ._utils import default_profile_dir, utc


@dataclass
class Account:
    """A TikTok account row.

    `username` is the login identifier you type into TikTok (email / phone /
    username) and the pool's primary key. It is *not* necessarily the public
    @handle — that is `unique_id`, resolved from the app-context after the
    first successful login. `user_id` (TikTok uid) is the ground-truth identity
    used to verify a Chrome profile is logged into the right account.

    `profile_dir` is a persistent Chrome user_data_dir (the working session).
    `cookies` is a JSON backup snapshot used to repair a logged-out / wrong
    profile without a manual re-login, and to answer "is this account active?"
    without launching a browser.
    """

    username: str
    password: str | None = None
    email: str | None = None
    email_password: str | None = None
    phone_number: str | None = None
    # Resolved on-platform identity (captured at first successful login).
    user_id: str | None = None
    sec_uid: str | None = None
    unique_id: str | None = None
    profile_dir: str | None = None
    active: bool = False
    locks: dict[str, datetime] = field(default_factory=dict)
    cookies: list[dict] = field(default_factory=list)
    proxy_server: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    error_msg: str | None = None
    last_used: datetime | None = None
    in_use: bool = False
    scrape_count_since_rest: int = 0
    scrape_count_overall_24h: int = 0

    def __post_init__(self):
        if not self.profile_dir:
            self.profile_dir = default_profile_dir(self.username)

    @property
    def identifier(self) -> str:
        return self.username

    @property
    def display_name(self) -> str:
        return self.unique_id or self.username

    @staticmethod
    def from_rs(rs: sqlite3.Row) -> "Account":
        doc = dict(rs)
        doc.pop("_tx", None)
        doc["locks"] = {k: utc.from_iso(v) for k, v in json.loads(doc["locks"]).items()}
        doc["cookies"] = json.loads(doc["cookies"])
        doc["active"] = bool(doc["active"])
        doc["in_use"] = bool(doc["in_use"])
        doc["last_used"] = utc.from_iso(doc["last_used"]) if doc["last_used"] else None
        return Account(**doc)

    def to_rs(self) -> dict:
        rs = asdict(self)
        rs["locks"] = json.dumps(rs["locks"], default=lambda x: x.isoformat())
        rs["cookies"] = json.dumps(rs["cookies"])
        rs["last_used"] = rs["last_used"].isoformat() if rs["last_used"] else None
        return rs

    def to_dict(self) -> dict:
        return asdict(self)
