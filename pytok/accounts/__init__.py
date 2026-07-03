"""Multi-account support for PyTok.

A SQLite-backed pool of TikTok accounts, each with a persistent Chrome profile
directory and a cookie backup, so multiple scraping sessions can run under
different identities. See pool.AccountsPool and account.Account.
"""

from .account import Account
from .pool import AccountsPool, NoAccountError
from .worker import Worker
from .worker_pool import WorkerPool
from ._utils import default_db_path, default_profile_dir, get_pytok_home

__all__ = [
    "Account",
    "AccountsPool",
    "NoAccountError",
    "Worker",
    "WorkerPool",
    "default_db_path",
    "default_profile_dir",
    "get_pytok_home",
]
