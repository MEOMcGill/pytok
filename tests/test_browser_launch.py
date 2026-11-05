"""Browser launch failures: orphaned profile processes, and telling an account's fault from
the host's. Offline: no browser, no network."""

import asyncio
import os
import subprocess
import sys
import tempfile
import time

import pytest

import pytok.tiktok
from pytok._profile import describe_profile, kill_profile_processes, profile_processes
from pytok.accounts import AccountsPool, WorkerPool
from pytok.accounts.worker import LaunchFaultTracker, Worker
from pytok.exceptions import BrowserLaunchException, HostBrowserLaunchException


def _spawn_on_profile(profile_dir):
    """A stand-in for a browser left running on `profile_dir`."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "-profile", profile_dir])


def _wait_until_visible(profile_dir, proc, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.pid in [p.pid for p in profile_processes(profile_dir)]:
            return
        time.sleep(0.05)
    raise AssertionError("spawned process never showed up with its -profile argument")


def test_orphaned_browser_on_a_profile_is_found_and_killed():
    profile = tempfile.mkdtemp()
    other = tempfile.mkdtemp()
    orphan = _spawn_on_profile(profile)
    bystander = _spawn_on_profile(other)
    try:
        _wait_until_visible(profile, orphan)
        assert str(orphan.pid) in describe_profile(profile)

        assert orphan.pid in kill_profile_processes(profile)
        assert orphan.wait(timeout=5) is not None
        assert bystander.poll() is None, "killed a browser on another profile"
        assert profile_processes(profile) == []
    finally:
        for p in (orphan, bystander):
            p.kill()
            p.wait()


def test_describe_profile_reports_lock_files():
    profile = tempfile.mkdtemp()
    open(os.path.join(profile, "parent.lock"), "w").close()
    line = describe_profile(profile)
    assert "parent.lock present" in line
    assert "browsers on this profile: none" in line


async def _pool(*names):
    pool = AccountsPool(db_file=os.path.join(tempfile.mkdtemp(), "accounts.db"))
    for name in names:
        await pool.add_account(name, cookies="sid=1")
        await pool.set_active(name, True)
    return pool


def test_tracker_blames_the_host_only_once_every_account_failed():
    async def scenario():
        pool = await _pool("acct-a", "acct-b")
        tracker = LaunchFaultTracker()
        err = BrowserLaunchException("boom")
        assert await tracker.record_failure(pool, "acct-a", err) is None
        tracker.record_success()
        assert await tracker.record_failure(pool, "acct-b", err) is None, \
            "a success in between should reset the count"
        fault = await tracker.record_failure(pool, "acct-a", err)
        assert isinstance(fault, HostBrowserLaunchException)

    asyncio.run(scenario())


class UnlaunchablePyTok:
    """A session whose browser never starts."""

    launches = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        UnlaunchablePyTok.launches += 1
        raise BrowserLaunchException("browser launch failed (profile x): exited")

    async def shutdown(self):
        pass


@pytest.fixture
def unlaunchable(monkeypatch):
    UnlaunchablePyTok.launches = 0
    monkeypatch.setattr(pytok.tiktok, "PyTok", UnlaunchablePyTok)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *a, **k: real_sleep(0))


def test_launch_failing_everywhere_fails_the_task_fast(unlaunchable):
    async def scenario():
        pool = await _pool("acct-a")
        worker = Worker(id="worker-test", pool=pool)
        assert await worker._acquire()

        async def task(api):
            raise AssertionError("ran without a session")

        with pytest.raises(HostBrowserLaunchException):
            await asyncio.wait_for(worker.execute_task(task), timeout=10)
        # one account's build attempts, not the in-place rebuilds on top of them
        assert UnlaunchablePyTok.launches == 3

    asyncio.run(scenario())


def test_pool_fails_queued_tasks_once_no_browser_starts(unlaunchable):
    async def scenario():
        pool = await _pool("acct-a")

        async def task(api):
            raise AssertionError("ran without a session")

        async with WorkerPool(pool, max_workers=1) as wp:
            results = await asyncio.wait_for(
                wp.run([task] * 4, return_exceptions=True), timeout=20)
        assert all(isinstance(r, HostBrowserLaunchException) for r in results)
        assert UnlaunchablePyTok.launches == 3

    asyncio.run(scenario())
