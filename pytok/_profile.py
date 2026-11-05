"""Inspecting and clearing a persistent Firefox profile before a browser launch."""

import logging
import os
import shutil
from typing import List

import psutil

logger = logging.getLogger("PyTok")

# Firefox's profile lock: parent.lock on Windows, a `lock` symlink plus .parentlock elsewhere.
LOCK_FILES = ("parent.lock", "lock", ".parentlock")


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def profile_processes(profile_dir: str) -> List[psutil.Process]:
    """Browser processes started with `-profile <profile_dir>`."""
    found = []
    for proc in psutil.process_iter(["cmdline"]):
        cmdline = proc.info.get("cmdline") or []
        for flag, value in zip(cmdline, cmdline[1:], strict=False):
            if flag in ("-profile", "--profile") and _same_path(value, profile_dir):
                found.append(proc)
                break
    return found


def kill_profile_processes(profile_dir: str, timeout: float = 5.0) -> List[int]:
    """Kill every browser still running on `profile_dir`, with its child processes.

    A browser left behind by an abandoned teardown keeps the profile locked, and Firefox
    then refuses to start on it. Returns the pids that were killed.
    """
    procs = []
    for proc in profile_processes(profile_dir):
        try:
            procs.extend([proc, *proc.children(recursive=True)])
        except psutil.Error:
            procs.append(proc)
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=timeout)
    return [p.pid for p in procs]


def _lock_state(path: str) -> str:
    if os.path.islink(path):
        return f"-> {os.readlink(path)}"
    try:
        with open(path, "rb"):
            return "present"
    except PermissionError:
        # Firefox holds parent.lock open without sharing, so only a live browser blocks a read.
        return "held by another process"
    except OSError as e:
        return f"unreadable ({e})"


def describe_profile(profile_dir: str) -> str:
    """One line on the state of a profile dir, for a failed launch's error message."""
    parts = [f"profile {profile_dir}"]
    if not os.path.isdir(profile_dir):
        return f"{parts[0]} does not exist"
    locks = [
        f"{name} {_lock_state(os.path.join(profile_dir, name))}"
        for name in LOCK_FILES
        if os.path.lexists(os.path.join(profile_dir, name))
    ]
    parts.append("locks: " + (", ".join(locks) if locks else "none"))
    try:
        pids = [p.pid for p in profile_processes(profile_dir)]
        parts.append(f"browsers on this profile: {pids or 'none'}")
    except Exception as e:
        parts.append(f"browsers on this profile: unknown ({e!r})")
    try:
        free = shutil.disk_usage(profile_dir).free
        parts.append(f"disk free {free / 2**30:.1f} GiB")
    except OSError:
        pass
    return "; ".join(parts)
