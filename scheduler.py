"""Simple persisted asyncio scheduler that enqueues jobs when due (FEAT3)."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

SCHEDULES_PATH = Path(__file__).resolve().parent / "osdd_schedules.json"
EVERY = ("daily", "weekly", "interval_minutes")
KINDS = ("download", "sync", "media")


def _parse_hhmm(value: Any) -> tuple:
    try:
        h, m = str(value).split(":")[:2]
        h, m = int(h), int(m)
    except (ValueError, TypeError):
        raise ValueError("الوقت يجب أن يكون بصيغة HH:MM")
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("الوقت يجب أن يكون بصيغة HH:MM")
    return h, m


def next_run(entry: dict, base: datetime) -> datetime:
    """First run time strictly after `base`."""
    every = entry["every"]
    if every == "interval_minutes":
        return base + timedelta(minutes=int(entry["minutes"]))
    h, m = _parse_hhmm(entry["time"])
    cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if every == "weekly":
        cand += timedelta(days=(int(entry.get("weekday", 0)) - cand.weekday()) % 7)
    while cand <= base:
        cand += timedelta(days=1 if every == "daily" else 7)
    return cand


class Scheduler:
    def __init__(self, enqueue: Callable[[str, dict], Any], path: Optional[Path] = None) -> None:
        self._enqueue = enqueue
        self._path = path or SCHEDULES_PATH
        self._task: Optional[asyncio.Task] = None
        self.entries: list = self._load()

    def _load(self) -> list:
        try:
            data = json.loads(Path(self._path).read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        Path(self._path).write_text(json.dumps(self.entries, ensure_ascii=False, indent=2), encoding="utf-8")

    def list(self) -> list:
        return [dict(e, next_run=self._next_iso(e)) for e in self.entries]

    def _next_iso(self, e: dict) -> str:
        base = datetime.fromtimestamp(e.get("last_run") or e.get("created") or time.time())
        try:
            return next_run(e, base).isoformat(timespec="minutes")
        except (KeyError, ValueError):
            return ""

    def add(self, body: dict) -> dict:
        kind, every = body.get("kind"), body.get("every")
        if kind not in KINDS:
            raise ValueError("kind يجب أن يكون download أو sync أو media")
        if every not in EVERY:
            raise ValueError("every يجب أن يكون daily أو weekly أو interval_minutes")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        entry: dict = {"id": uuid.uuid4().hex[:8], "kind": kind, "params": params, "every": every,
                       "created": time.time(), "last_run": None}
        if every == "interval_minutes":
            try:
                minutes = int(body.get("minutes") or body.get("time"))
            except (TypeError, ValueError):
                raise ValueError("minutes مطلوب لنوع interval_minutes")
            entry["minutes"] = max(1, minutes)
            entry["time"] = ""
        else:
            _parse_hhmm(body.get("time"))
            entry["time"] = str(body["time"])
            if every == "weekly":
                entry["weekday"] = int(body.get("weekday", 0)) % 7
        self.entries.append(entry)
        self._save()
        return dict(entry, next_run=self._next_iso(entry))

    def remove(self, entry_id: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e["id"] != entry_id]
        if len(self.entries) != before:
            self._save()
            return True
        return False

    def tick(self, now: Optional[datetime] = None) -> list:
        """Enqueue due entries, return their ids."""
        now = now or datetime.now()
        fired = []
        for e in self.entries:
            base = datetime.fromtimestamp(e.get("last_run") or e.get("created") or now.timestamp())
            try:
                due = next_run(e, base) <= now
            except (KeyError, ValueError):
                continue
            if due:
                e["last_run"] = now.timestamp()
                fired.append(e["id"])
                try:
                    self._enqueue(e["kind"], dict(e["params"]))
                except Exception:
                    pass
        if fired:
            self._save()
        return fired

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                self.tick()
            except Exception:
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
