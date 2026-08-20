"""Worker/pool teardown behaviour, on a temporary accounts DB and a fake session.

Offline: no browser, no network. Async bodies are driven with asyncio.run so the suite
needs no pytest-asyncio.
"""

import asyncio
import os
import tempfile

import pytest

from pytok.accounts import AccountsPool
from pytok.accounts import worker as worker_mod
from pytok.accounts.worker import Worker


class HangingSession:
    """A session whose teardown never returns, as a dead browser's does."""

    async def shutdown(self):
        await asyncio.Event().wait()


async def _pool_with_one_account():
    db_file = os.path.join(tempfile.mkdtemp(), "accounts.db")
    pool = AccountsPool(db_file=db_file)
    await pool.add_account("acct-a", cookies="sid=1")
    await pool.set_active("acct-a", True)
    return pool


def test_hung_teardown_still_releases_the_account(monkeypatch):
    """A worker that cannot close its browser must not keep the account.

    The account is only reclaimable through the release in rotate_account, so a teardown
    that hangs there takes the whole pool down with it: every worker then waits on an
    account nothing will ever hand back.
    """

    monkeypatch.setattr(worker_mod, "SESSION_CLOSE_TIMEOUT", 2)

    async def scenario():
        pool = await _pool_with_one_account()
        worker = Worker(id="worker-test", pool=pool)
        assert await worker._acquire()
        assert (await pool.stats())["in_use"] == 1
        worker.api = HangingSession()

        # rotate_account re-acquires at the end; stub that out to isolate the release
        async def no_reacquire(wait=False):
            return False

        worker._acquire = no_reacquire

        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(worker_mod.NoAccountError):
            await asyncio.wait_for(worker.rotate_account(cooldown_minutes=0), timeout=20)
        elapsed = loop.time() - started

        assert elapsed < 10, "the teardown was waited on instead of abandoned"
        assert (await pool.stats())["in_use"] == 0, "account left in_use"
        assert worker.api is None

        await asyncio.sleep(1.5)  # let the 0-minute cooldown lapse
        assert await pool.get_available_or_wait(poll=0.2) is not None

    asyncio.run(scenario())


def test_waiting_for_an_account_keeps_reporting(caplog):
    """A wait that outlives every cooldown is a stall, and has to look like one.

    Reported once, it is indistinguishable in the log from a run that finished quietly.
    """

    async def scenario():
        pool = await _pool_with_one_account()
        assert await pool.get_available() is not None       # hold it, in_use
        await pool.lock_until("acct-a", "datetime('now', '+1 minutes')")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                pool.get_available_or_wait(poll=0.2, remind_after=0.5), timeout=2
            )

    with caplog.at_level("WARNING", logger="PyTok"):
        asyncio.run(scenario())

    reminders = [r for r in caplog.records if "Still no account" in r.message]
    assert reminders, "a stuck wait reported nothing after the first line"
    assert "'in_use': 1" in reminders[0].message, "the reminder omits what is holding it"


if __name__ == "__main__":
    pytest.main([__file__])
