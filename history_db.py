"""SQLite job history (FEAT5)."""
from __future__ import annotations

import csv
import io
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent / "osdd_history.db"

_COLS = ["id", "kind", "source", "dest", "started", "finished", "status", "files", "bytes", "errors"]


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, source TEXT,"
        " dest TEXT, started REAL, finished REAL, status TEXT, files INTEGER DEFAULT 0,"
        " bytes INTEGER DEFAULT 0, errors TEXT DEFAULT '')"
    )
    return con


def start_job(kind: str, source: str = "", dest: str = "") -> int:
    con = _conn()
    try:
        cur = con.execute(
            "INSERT INTO jobs(kind,source,dest,started,status) VALUES(?,?,?,?,?)",
            (kind, source or "", dest or "", time.time(), "running"),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def finish_job(job_id: int, status: str, files: int = 0, nbytes: int = 0, errors: str = "") -> None:
    con = _conn()
    try:
        con.execute(
            "UPDATE jobs SET finished=?, status=?, files=?, bytes=?, errors=? WHERE id=?",
            (time.time(), status, int(files or 0), int(nbytes or 0), str(errors or "")[:2000], job_id),
        )
        con.commit()
    finally:
        con.close()


def list_jobs(limit: int = 100) -> list:
    limit = max(1, min(5000, int(limit)))
    con = _conn()
    try:
        rows = con.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def clear() -> int:
    con = _conn()
    try:
        n = con.execute("DELETE FROM jobs").rowcount
        con.commit()
        return n
    finally:
        con.close()


def export_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_COLS)
    for r in reversed(list_jobs(5000)):
        w.writerow([r.get(c, "") for c in _COLS])
    return buf.getvalue()


class JobTracker:
    """Turns polled manager snapshots into history rows + finish callbacks."""

    TERMINAL = {"completed", "failed", "cancelled", "error"}
    ACTIVE = {"running", "paused", "scanning"}

    def __init__(self, on_finish: Optional[Any] = None) -> None:
        self._active: dict = {}
        self._seen: dict = {}
        self.on_finish = on_finish

    def observe(self, kind: str, snap: dict) -> None:
        status = str(snap.get("status") or "idle")
        key = snap.get("job_id")
        source = str(snap.get("source_dir") or snap.get("url") or snap.get("mode") or kind)
        dest = str(snap.get("dest_dir") or "")
        cur = self._active.get(kind)
        if cur and key is not None and cur[1] != key:  # a new job replaced the old one
            self._finish(kind, "cancelled", snap)
            cur = None
        if status in self.ACTIVE and cur is None:
            self._active[kind] = (start_job(kind, source, dest), key)
        elif status in self.TERMINAL:
            if cur is None and key is not None and self._seen.get(kind) != (key, status):
                self._active[kind] = (start_job(kind, source, dest), key)  # short job we missed
            if kind in self._active:
                self._finish(kind, "failed" if status == "error" else status, snap)
            self._seen[kind] = (key, status)
        elif status == "idle" and cur is not None:
            self._finish(kind, "cancelled", snap)

    def _finish(self, kind: str, status: str, snap: dict) -> None:
        job_id, _ = self._active.pop(kind)
        files = snap.get("completed_files", snap.get("copied_files", 0))
        nbytes = snap.get("downloaded_bytes", snap.get("copied_bytes", 0))
        err = snap.get("error") or ""
        if not err and snap.get("failed_files"):
            err = f"{snap.get('failed_files')} failed"
        finish_job(job_id, status, files, nbytes, err)
        if self.on_finish:
            try:
                self.on_finish(kind, status, {"files": files, "bytes": nbytes, "errors": err,
                                              "dest": snap.get("dest_dir") or ""})
            except Exception:
                pass
