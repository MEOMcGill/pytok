"""Small helpers for the accounts pool (kept free of pandas/polars imports)."""

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def get_pytok_home() -> str:
    """Base directory for PyTok state (accounts DB + Chrome profiles).

    Override with the PYTOK_HOME env var; defaults to ~/.pytok.
    """
    return os.environ.get("PYTOK_HOME", os.path.join(str(Path.home()), ".pytok"))


def default_db_path() -> str:
    return os.path.join(get_pytok_home(), "accounts.db")


def default_profile_dir(username: str) -> str:
    """Per-account persistent Chrome user_data_dir.

    Filesystem-safe slug of the login identifier under <home>/profiles/.
    """
    safe = "".join(c if c.isalnum() or c in "-._@" else "_" for c in username)
    return os.path.join(get_pytok_home(), "profiles", safe)


def get_env_bool(key: str, default_val: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default_val
    return val.lower() in ("1", "true", "yes")


class utc:
    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def from_iso(iso: str) -> datetime:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)

    @staticmethod
    def ts() -> int:
        return int(utc.now().timestamp())


def parse_cookies(val) -> list[dict]:
    """Normalise cookies from various inputs into a CDP-style cookie list.

    Accepts: a Python list of cookie dicts, a JSON string (list, or dict with a
    "cookies" key, or a flat {name: value} map), a base64-wrapped JSON blob, or
    a raw "name=value; name2=value2" header string. Missing domain defaults to
    the TikTok cookie domain so injected cookies attach to tiktok.com.
    """
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        if "cookies" in val:
            return val["cookies"]
        return [_cookie_kv(name, value) for name, value in val.items()]

    if not isinstance(val, str):
        raise ValueError(f"Invalid cookie value of type {type(val)}")

    val = val.strip()
    if not val:
        return []

    # 1) JSON (a list, or a dict that is either {"cookies": [...]} or {name: value})
    if val[:1] in "[{":
        res = json.loads(val)
        if isinstance(res, dict) and "cookies" in res:
            return res["cookies"]
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return [_cookie_kv(name, value) for name, value in res.items()]

    # 2) base64-wrapped JSON (common in exported cookie blobs). validate=True so
    #    a plain "name=value; ..." header (which has spaces / mid-string '=') is
    #    rejected here rather than silently mangled.
    try:
        decoded = base64.b64decode(val, validate=True).decode()
        if decoded.strip()[:1] in "[{":
            return parse_cookies(decoded)
    except Exception:
        pass

    # 3) raw "name=value; name2=value2" cookie header
    pairs = [x.split("=", 1) for x in val.split(";") if "=" in x]
    if pairs:
        return [_cookie_kv(k.strip(), v.strip()) for k, v in pairs]

    raise ValueError(f"Invalid cookie value: {val[:80]}")


def _cookie_kv(name: str, value: str) -> dict:
    return {
        "name": name,
        "value": value,
        "domain": ".tiktok.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "None",
    }
