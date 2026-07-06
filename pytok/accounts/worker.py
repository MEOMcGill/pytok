"""Worker: owns one account + its PyTok/Chrome session, runs tasks on it.

A *task* is a user callable ``async def task(api: PyTok) -> result``. The worker
acquires an account from the pool, lazily builds and enters a PyTok bound to it
(persistent Chrome profile), and reuses that session across tasks until a
rotation/rest, a crash, or shutdown. Failures are routed by a pytok-flavoured
taxonomy — data-level errors propagate to the caller, account/session-level
errors trigger a cooldown + rotation, logouts trigger a session rebuild.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from .account import Account
from .pool import AccountsPool, NoAccountError
from ..exceptions import (
    AccountPrivateException,
    ApiFailedException,
    CaptchaException,
    EmptyResponseException,
    FewerVideosThanExpectedException,
    InvalidJSONException,
    LoginException,
    NoContentException,
    NotAvailableException,
    NotFoundException,
    SoundRemovedException,
    TimeoutException,
)

logger = logging.getLogger("PyTok")

PyTokTask = Callable[[Any], Awaitable[Any]]

# ---- Failure taxonomy -------------------------------------------------------
# Data-level: the target content just isn't available; not the account's fault.
# Propagate to the caller unchanged — no retry, no rotation.
DATA_LEVEL_EXCEPTIONS = (
    NotFoundException,
    NotAvailableException,
    AccountPrivateException,
    EmptyResponseException,
    NoContentException,
    SoundRemovedException,
    InvalidJSONException,
    FewerVideosThanExpectedException,
)
# Account/session-level & transient: cooldown the current account and rotate,
# then retry the task on the next available account.
ROTATE_EXCEPTIONS = (
    CaptchaException,
    ApiFailedException,
    TimeoutException,
)

# Rotation / rest policy.
DEFAULT_TASKS_PER_REST = None  # None => never force a rest on task count
REST_MINUTES = 5
RATE_LIMIT_MINUTES = 15


class Worker:
    """Runs tasks for one acquired account, rotating/rebuilding as needed."""

    def __init__(
        self,
        id: str,
        pool: AccountsPool,
        tasks_per_rest: Optional[int] = DEFAULT_TASKS_PER_REST,
        max_retries: int = 3,
        pytok_kwargs: Optional[dict] = None,
        startup_delay: float = 0.0,
    ):
        self.id = id
        self.pool = pool
        self.tasks_per_rest = tasks_per_rest
        self.max_retries = max_retries
        self.pytok_kwargs = pytok_kwargs or {}
        # One-shot delay before this worker builds its FIRST session. The pool
        # staggers workers (worker-0: 0s, worker-1: Ns, ...) so their browser
        # startups don't overlap. Each worker has its OWN browser/tab/session
        # (nothing is shared between workers), but launching two zendriver
        # Chrome instances at the same instant intermittently leaves one
        # PyTok's API client without a created session ("No sessions created"
        # on the first request). Mitigation, not a proven root-cause fix; the
        # in-place session rebuild in execute_task is what actually recovers it.
        self.startup_delay = startup_delay
        self._startup_delayed = False

        self.current_account: Optional[Account] = None
        self.tasks_done: int = 0
        self.api = None  # PyTok, built lazily and reused across tasks
        self._initialized = False

    def _acct_name(self) -> str:
        return self.current_account.username if self.current_account else "<none>"

    @classmethod
    async def create(cls, id: str, pool: AccountsPool, **kwargs) -> "Worker":
        worker = cls(id=id, pool=pool, **kwargs)
        if not await worker._acquire():
            raise NoAccountError(f"Worker {id}: no account available")
        return worker

    async def _acquire(self, wait: bool = False) -> bool:
        account = await (
            self.pool.get_available_or_wait() if wait else self.pool.get_available()
        )
        if not account:
            return False
        self.current_account = account
        self.tasks_done = 0
        self._initialized = True
        logger.info(f"Worker {self.id} acquired account {account.username}")
        return True

    async def _ensure_session(self, max_build_attempts: int = 3):
        """Build + enter the PyTok session for the current account (reused
        across tasks).

        Transient build failures rebuild on the SAME account (browser launches
        are serialized by PyTok's shared startup_lock, so concurrent workers no
        longer race here). Only bad credentials (LoginException) rotate to
        another account. Bounded by max_build_attempts so a persistently-broken
        account can't spin the worker in an endless open/close loop."""
        from ..tiktok import PyTok

        # Optional extra startup jitter (default 0; the shared startup_lock is
        # what actually serializes concurrent browser launches now).
        if not self._startup_delayed:
            self._startup_delayed = True
            if self.startup_delay:
                logger.info(f"Worker {self.id}: staggering first session build "
                            f"by {self.startup_delay:.0f}s")
                await asyncio.sleep(self.startup_delay)

        last_exc: Optional[Exception] = None
        attempts = 0
        while self.api is None:
            if attempts >= max_build_attempts:
                raise RuntimeError(
                    f"Worker {self.id}: could not build a session for "
                    f"{self._acct_name()} after {attempts} attempts"
                ) from last_exc
            attempts += 1
            if self.current_account is None and not await self._acquire(wait=True):
                raise NoAccountError(f"Worker {self.id}: no account for session")
            api = PyTok(
                account=self.current_account,
                accounts_pool=self.pool,
                release_on_shutdown=False,
                **self.pytok_kwargs,
            )
            try:
                await api.__aenter__()
                self.api = api
            except LoginException as e:
                # Bad/expired credentials for this account: PyTok already tore
                # itself down (but did not release, since release_on_shutdown is
                # False). Mark inactive, release, and rotate to another account.
                logger.warning(f"Worker {self.id}: session login failed for "
                               f"{self.current_account.username}: {e}")
                await self.pool.mark_inactive(self.current_account.username, f"Login failed: {e}")
                await self.pool.release_account(self.current_account.username)
                self.current_account = None
            except Exception as e:
                # Transient / unknown build failure (e.g. a browser-startup race,
                # a slow cold start). PyTok closes its own browser if __aenter__
                # fails inside the account-verification block, but a failure in
                # the browser-launch phase leaves a live browser — shut it down
                # defensively so we don't orphan it.
                #
                # Rebuild on the SAME account rather than releasing + rotating:
                # the account isn't at fault, and releasing here makes N workers
                # thrash each other's accounts when they fail concurrently
                # (turning a transient blip into pool-wide churn). Keeping the
                # account pinned lets each worker recover independently. A short
                # backoff spaces out retries so a dying browser fully releases
                # before the next launch. A persistently-unbuildable account is
                # still bounded by max_build_attempts above.
                last_exc = e
                logger.error(f"Worker {self.id}: session build failed for "
                             f"{self._acct_name()}: {e!r}; rebuilding on same account "
                             f"(attempt {attempts}/{max_build_attempts})")
                try:
                    await api.shutdown()
                except Exception:
                    pass
                await asyncio.sleep(min(2 ** attempts, 10))
        return self.api

    async def execute_task(self, task: PyTokTask) -> Any:
        """Run one task on this worker's session, applying the failure taxonomy.

        Retries transient/account-level failures on rotated accounts up to
        max_retries. Data-level exceptions propagate immediately.
        """
        if self.tasks_per_rest and self.tasks_done >= self.tasks_per_rest:
            logger.info(f"Worker {self.id}: {self.tasks_done} tasks done, resting "
                        f"{self._acct_name()}")
            await self.rotate_account(REST_MINUTES)

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                api = await self._ensure_session()
                result = await task(api)
                await self.pool.increment_scrape_count(self.current_account.username)
                self.tasks_done += 1
                return result

            except NoAccountError:
                # Pool is exhausted (all accounts in_use / inactive / locked).
                # Retrying won't conjure one — surface it to the caller.
                raise

            except DATA_LEVEL_EXCEPTIONS:
                # Not the account's fault — let the caller handle it.
                raise

            except ROTATE_EXCEPTIONS as e:
                last_exc = e
                minutes = RATE_LIMIT_MINUTES if isinstance(e, ApiFailedException) else REST_MINUTES
                logger.warning(f"Worker {self.id}: {type(e).__name__} on "
                               f"{self._acct_name()} (attempt {attempt + 1}"
                               f"/{self.max_retries}); cooldown {minutes}m + rotate")
                await self.rotate_account(minutes)

            except LoginException as e:
                # Session logged out mid-task. Rebuild once (repairs from cookie
                # backup / re-login via _verify_account); if it keeps failing,
                # _ensure_session will mark it inactive and rotate.
                last_exc = e
                logger.warning(f"Worker {self.id}: logged out on "
                               f"{self._acct_name()}; rebuilding session")
                await self._close_session()

            except Exception as e:
                # Unknown error / browser crash / transient session-setup race
                # (e.g. "No sessions created" when two browsers start at once).
                # Drop the (probably dead) session and rebuild IN PLACE on the
                # same account — these recover on a fresh session in seconds.
                # We deliberately do NOT rotate+cooldown here: with a small pool
                # (e.g. 2 accounts) rotating just locks this account for minutes
                # and waits on the other busy one, turning a transient blip into
                # a multi-minute stall. Genuine rate-limit/captcha/timeout still
                # rotate via ROTATE_EXCEPTIONS above; a persistently unbuildable
                # account is caught by _ensure_session's max_build_attempts.
                last_exc = e
                logger.error(f"Worker {self.id}: unexpected error on "
                             f"{self._acct_name()}: {e!r}; rebuilding session in place")
                await self._close_session()

        raise RuntimeError(
            f"Worker {self.id}: task failed after {self.max_retries} attempts"
        ) from last_exc

    async def rotate_account(self, cooldown_minutes: int = REST_MINUTES):
        """Cooldown the current account and switch to the next available one.

        With a small pool this often re-acquires the same account after its
        cooldown expires (via get_available_or_wait), giving the periodic-rest
        behaviour rather than crashing.
        """
        await self._close_session()
        if self.current_account:
            await self.pool.lock_until(
                self.current_account.username,
                f"datetime('now', '+{cooldown_minutes} minutes')",
            )
            await self.pool.release_account(self.current_account.username)
            logger.info(f"Worker {self.id} rested {self.current_account.username} "
                        f"({cooldown_minutes}m)")
            self.current_account = None
        self.tasks_done = 0
        self._initialized = False

        if not await self._acquire(wait=True):
            raise NoAccountError(f"Worker {self.id}: no account for rotation")

    async def _close_session(self):
        if self.api is not None:
            try:
                await self.api.shutdown()  # release_on_shutdown=False: keeps account
            except Exception:
                pass
            self.api = None

    async def _release_current(self):
        if self.current_account:
            try:
                await self.pool.release_account(self.current_account.username)
            except Exception:
                pass

    async def close(self):
        """Tear down the session and release the account back to the pool."""
        await self._close_session()
        await self._release_current()
        self.current_account = None
        self.tasks_done = 0
        self._initialized = False
