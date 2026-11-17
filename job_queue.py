"""In-memory priority job queue, runs one job at a time (FEAT2)."""
from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any, Awaitable, Callable, Optional

KINDS = ("download", "sync", "media")


class JobQueue:
    """runner(kind, params) -> awaitable that starts the job and waits for it to finish.
    canceller(kind) stops the running job. Higher priority runs first, FIFO within."""

    def __init__(
        self,
        runner: Callable[[str, dict], Awaitable[Any]],
        canceller: Optional[Callable[[str], Awaitable[Any]]] = None,
    ) -> None:
        self._runner = runner
        self._canceller = canceller
        self._jobs: dict = {}
        self._seq = itertools.count(1)
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._stop = False

    def add(self, kind: str, params: dict, priority: int = 0, source: str = "manual") -> dict:
        if kind not in KINDS:
            raise ValueError(f"نوع غير مدعوم: {kind}")
        n = next(self._seq)
        job = {
            "id": f"q{n}", "seq": n, "kind": kind, "params": params or {}, "priority": int(priority),
            "status": "pending", "source": source, "added": time.time(),
            "started": None, "finished": None, "error": "",
        }
        self._jobs[job["id"]] = job
        if self._wake:
            self._wake.set()
        return self._public(job)

    @staticmethod
    def _public(job: dict) -> dict:
        return {k: v for k, v in job.items() if k != "seq"}

    def list(self) -> list:
        jobs = sorted(self._jobs.values(), key=lambda j: (j["status"] != "running", j["status"] != "pending",
                                                          -j["priority"], j["seq"]))
        return [self._public(j) for j in jobs]

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        if job["status"] == "pending":
            job["status"] = "cancelled"
            job["finished"] = time.time()
            return True
        if job["status"] == "running":
            job["status"] = "cancelling"
            if self._canceller:
                await self._canceller(job["kind"])
            return True
        return False

    def _next(self) -> Optional[dict]:
        pend = [j for j in self._jobs.values() if j["status"] == "pending"]
        return min(pend, key=lambda j: (-j["priority"], j["seq"])) if pend else None

    async def _loop(self) -> None:
        assert self._wake is not None
        while not self._stop:
            job = self._next()
            if job is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            job["status"], job["started"] = "running", time.time()
            try:
                await self._runner(job["kind"], job["params"])
                if job["status"] == "cancelling":
                    job["status"] = "cancelled"
                else:
                    job["status"] = "done"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                job["status"] = "failed"
                job["error"] = str(exc)[:300]
            job["finished"] = time.time()
            self._prune()

    def _prune(self, keep: int = 100) -> None:
        done = [j for j in self._jobs.values() if j["status"] in ("done", "failed", "cancelled")]
        done.sort(key=lambda j: j["seq"])
        for j in done[:-keep]:
            self._jobs.pop(j["id"], None)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = False
            self._wake = asyncio.Event()
            self._task = asyncio.create_task(self._loop())
            if self._next():
                self._wake.set()

    async def shutdown(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
