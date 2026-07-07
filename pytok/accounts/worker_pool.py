"""WorkerPool: run tasks concurrently across a pool of account-bound sessions.

Each Worker owns its own PyTok/Chrome instance (one per account, isolated by the
account's profile dir), so N workers = N concurrent scraping sessions. Tasks are
user callables ``async def task(api: PyTok) -> result`` submitted to a shared
queue; the pool returns a Future per task (or gathers a batch via ``run``).

    from pytok.accounts import AccountsPool, WorkerPool

    async def scrape_user(api, handle):
        videos = []
        async for v in api.user(username=handle).videos(count=100):
            videos.append(v.as_dict)
        return videos

    pool = AccountsPool()
    async with WorkerPool(pool, max_workers=2, headless=False) as wp:
        results = await wp.run([
            lambda api, h=h: scrape_user(api, h)
            for h in ["therock", "khaby.lame", "charlidamelio"]
        ])
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, List, Optional

from .pool import AccountsPool, NoAccountError
from .worker import PyTokTask, Worker

logger = logging.getLogger("PyTok")


class WorkerPool:
    def __init__(
        self,
        pool: AccountsPool,
        max_workers: int = 4,
        tasks_per_rest: Optional[int] = None,
        max_retries: int = 3,
        startup_stagger: float = 0.0,
        shutdown_grace: float = 30.0,
        **pytok_kwargs,
    ):
        """
        Args:
            pool: the AccountsPool to draw accounts from.
            max_workers: upper bound on concurrent sessions. The pool uses
                min(max_workers, number of active accounts) workers.
            tasks_per_rest: rest+rotate a worker's account after this many tasks
                (None = never force a rest).
            max_retries: per-task retry budget across rotated accounts.
            startup_stagger: optional extra delay (seconds) before each worker's
                FIRST session build (worker-i waits i * startup_stagger). Left at
                0 by default: browser launches are now serialized deterministically
                by a shared startup lock (see below), so a fixed stagger is no
                longer needed to keep concurrent Chrome starts from racing.
            shutdown_grace: seconds close() waits for in-flight worker loops to
                finish before cancelling them. Idle workers exit within ~1s, so
                this only matters when close() runs while a task is still going
                (e.g. an exception or timeout escaped the `async with` body) —
                without a bound, a worker stuck waiting out an account cooldown
                would block teardown for minutes.
            **pytok_kwargs: forwarded to each PyTok (headless, request_delay,
                page_load_timeout, manual_captcha_solves, ...). If logging_level
                is not given, workers default to this module's "PyTok" logger
                effective level, so worker-session INFO logs stay visible when
                the app configured INFO logging (each PyTok sets the shared
                "PyTok" logger level, so a WARNING default would silence the
                pool's own progress logs too).
        """
        self.pool = pool
        self.max_workers = max_workers
        self.tasks_per_rest = tasks_per_rest
        self.max_retries = max_retries
        self.startup_stagger = startup_stagger
        self.shutdown_grace = shutdown_grace
        self.pytok_kwargs = pytok_kwargs

        self.workers: List[Worker] = []
        self.worker_tasks: List[asyncio.Task] = []
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self._initialized = False
        self._shutdown = False
        self._init_lock = asyncio.Lock()
        # Shared by every worker's PyTok to serialize the racy browser-launch
        # phase (zendriver start + session bind). Created lazily in initialize()
        # so it binds to the running loop. This — not startup_stagger — is what
        # keeps N concurrent Chrome starts from racing each other's session setup.
        self._startup_lock: Optional[asyncio.Lock] = None

    async def initialize(self) -> int:
        if self._initialized:
            return len(self.workers)

        active = await self.pool.get_active_accounts()
        if not active:
            raise NoAccountError("No active accounts in pool")

        num = max(1, min(self.max_workers, len(active)))
        logger.info(f"WorkerPool initializing {num} worker(s) "
                    f"(max={self.max_workers}, active={len(active)})")

        # One lock shared by all workers' PyTok instances, so only one browser
        # launch (first build or mid-run rebuild) runs at a time.
        self._startup_lock = asyncio.Lock()
        self.pytok_kwargs = {**self.pytok_kwargs, "startup_lock": self._startup_lock}
        # Default worker sessions to the pool logger's verbosity: every PyTok
        # sets the shared "PyTok" logger's level, so letting workers fall back
        # to PyTok's WARNING default would silence the pool's own INFO logs.
        self.pytok_kwargs.setdefault("logging_level", logger.getEffectiveLevel())

        for i in range(num):
            try:
                worker = await Worker.create(
                    id=f"worker-{i}",
                    pool=self.pool,
                    tasks_per_rest=self.tasks_per_rest,
                    max_retries=self.max_retries,
                    pytok_kwargs=self.pytok_kwargs,
                    startup_delay=i * self.startup_stagger,
                )
            except NoAccountError:
                logger.warning(f"WorkerPool: only created {len(self.workers)}/{num} workers")
                break
            self.workers.append(worker)
            self.worker_tasks.append(asyncio.create_task(self._worker_loop(worker)))

        if not self.workers:
            raise NoAccountError("Failed to create any workers")

        self._initialized = True
        return len(self.workers)

    async def _worker_loop(self, worker: Worker):
        logger.info(f"{worker.id} loop started")
        while not self._shutdown:
            try:
                task, future = await asyncio.wait_for(self.task_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                if future.cancelled():
                    continue
                result = await worker.execute_task(task)
                if not future.cancelled():
                    future.set_result(result)
            except asyncio.CancelledError:
                # close() cancelled us mid-task (shutdown_grace expired).
                # Resolve the in-flight future so no caller awaits it forever;
                # the finally below still marks the queue item done.
                if not future.done():
                    future.set_exception(
                        RuntimeError(f"{worker.id}: pool shut down before task completed")
                    )
                raise
            except Exception as e:
                if not future.cancelled():
                    future.set_exception(e)
            finally:
                self.task_queue.task_done()
        logger.info(f"{worker.id} loop exiting")

    async def submit(self, task: PyTokTask) -> asyncio.Future:
        """Enqueue a task; returns a Future resolved with its result/exception."""
        async with self._init_lock:
            if not self._initialized:
                await self.initialize()
        future = asyncio.get_running_loop().create_future()
        await self.task_queue.put((task, future))
        return future

    async def run(self, tasks: List[PyTokTask], return_exceptions: bool = False) -> List[Any]:
        """Submit a batch of tasks and gather their results in order."""
        futures = [await self.submit(t) for t in tasks]
        return await asyncio.gather(*futures, return_exceptions=return_exceptions)

    async def close(self):
        if not self._initialized:
            return
        self._shutdown = True
        if self.worker_tasks:
            # Idle loops notice _shutdown within ~1s. A loop still inside
            # execute_task (e.g. waiting out an account cooldown) gets
            # shutdown_grace seconds to finish, then is cancelled so teardown
            # can't hang on a stuck worker.
            done, pending = await asyncio.wait(
                self.worker_tasks, timeout=self.shutdown_grace
            )
            if pending:
                logger.warning(
                    f"WorkerPool.close: cancelling {len(pending)} worker loop(s) "
                    f"still busy after {self.shutdown_grace:.0f}s grace"
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(*self.worker_tasks, return_exceptions=True)
        for worker in self.workers:
            await worker.close()
        self.workers = []
        self.worker_tasks = []
        self._initialized = False
        self._shutdown = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False
