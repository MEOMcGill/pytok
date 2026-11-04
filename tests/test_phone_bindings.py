"""Binding pool accounts to phones, on a temporary accounts DB. Offline."""

import os
import sqlite3
import tempfile

import pytest

from pytok.accounts import AccountsPool
from pytok.phone import PhoneError, PhoneTikTok


async def _pool(*usernames):
    pool = AccountsPool(db_file=os.path.join(tempfile.mkdtemp(), "accounts.db"))
    for username in usernames:
        await pool.add_account(username, cookies="sid=1")
        await pool.set_active(username, True)
    return pool


async def test_bound_account_is_acquired_for_its_phone_only():
    pool = await _pool("phone-acct", "desk-acct")
    await pool.bind_phone("phone-acct", "SERIAL1")
    assert (await pool.get_for_phone("SERIAL1")).username == "phone-acct"
    assert await pool.get_for_phone("SERIAL2") is None


async def test_desktop_acquisition_skips_phone_bound_accounts():
    pool = await _pool("phone-acct")
    await pool.bind_phone("phone-acct", "SERIAL1")
    assert await pool.get_available() is None


async def test_rebinding_replaces_both_sides():
    pool = await _pool("a", "b")
    await pool.bind_phone("a", "S1")
    await pool.bind_phone("b", "S1")
    await pool.bind_phone("b", "S2")
    assert await pool.phone_bindings() == {"S2": "b"}


async def test_binding_an_unknown_account_fails():
    pool = await _pool()
    with pytest.raises(ValueError):
        await pool.bind_phone("nobody", "S1")


async def test_accounts_table_keeps_its_columns():
    """Older pytok builds turn accounts rows into Account(**row), so no new columns."""
    pool = await _pool("a")
    await pool.bind_phone("a", "S1")
    with sqlite3.connect(pool._db_file) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(accounts)")]
    assert "serial" not in columns and "phone_serial" not in columns


async def test_from_pool_holds_the_bound_account_until_released():
    pool = await _pool("phone-acct")
    await pool.bind_phone("phone-acct", "S1")
    phone = await PhoneTikTok.from_pool(pool, "S1")
    assert phone.account.username == "phone-acct"
    with pytest.raises(PhoneError, match="in use"):
        await PhoneTikTok.from_pool(pool, "S1")
    await phone._release_account()
    assert (await PhoneTikTok.from_pool(pool, "S1")).account.username == "phone-acct"


async def test_from_pool_names_an_unbound_phone():
    pool = await _pool("a")
    with pytest.raises(PhoneError, match="No account is bound"):
        await PhoneTikTok.from_pool(pool, "S9")
