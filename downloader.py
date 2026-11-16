"""Async file-by-file SharePoint download engine with Range resume, anti-HTML protection, and live SSE snapshots."""

from __future__ import annotations

import asyncio
import email.utils
import json
import os
import random
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from sp_auth import LOGIN_HOSTS
from sp_client import SharePointClient
from utils import build_local_path, win_long_path

MANIFEST_FILENAME = ".sp_downloader_manifest.json"
HTML_ALLOWED_EXTENSIONS = {".html", ".htm", ".aspx", ".mht", ".mhtml"}

_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)
_CONTENT_RANGE_416_RE = re.compile(r"^bytes\s+\*/(\d+)$", re.IGNORECASE)


class _JobPausedError(Exception):
    """Internal signal that the active stream was interrupted due to pause."""


class _JobCancelledError(Exception):
    """Internal signal that the active stream was interrupted due to cancel."""


def _is_login_redirect(url: str) -> bool:
    """Detect redirects to Microsoft/ADFS login or SharePoint authentication endpoints."""
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if any(h in host for h in (*LOGIN_HOSTS, "login.microsoftonline.com", "login.live.com")):
        return True
    if "authenticate.aspx" in path or "/adfs/ls" in path:
        return True
    return False


def _is_unexpected_html(filename: str, content_type: Optional[str]) -> bool:
    """Reject HTML/XHTML error or sign-in pages when the target file is not an HTML/ASPX document."""
    ext = Path(filename).suffix.lower()
    if ext in HTML_ALLOWED_EXTENSIONS:
        return False
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct in ("text/html", "application/xhtml+xml")


def _parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    """Parse numeric seconds or HTTP-date from a Retry-After header."""
    if not header_value:
        return None
    raw = header_value.strip()
    if not raw:
        return None
    try:
        secs = float(raw)
        return max(0.0, secs)
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def _parse_content_range(header_value: Optional[str]) -> Optional[tuple[int, int, Optional[int]]]:
    """Parse `Content-Range: bytes <start>-<end>/<total|*>` into (start, end, total)."""
    if not header_value:
        return None
    m = _CONTENT_RANGE_RE.match(header_value.strip())
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2))
    total = int(m.group(3)) if m.group(3) != "*" else None
    if start > end:
        return None
    if total is not None and end >= total:
        return None
    return start, end, total


def _parse_416_total(header_value: Optional[str]) -> Optional[int]:
    """Parse `Content-Range: bytes */<total>` from a 416 response."""
    if not header_value:
        return None
    m = _CONTENT_RANGE_416_RE.match(header_value.strip())
    if not m:
        return None
    return int(m.group(1))


def _dedupe_urls(urls: list[Optional[str]]) -> list[str]:
    """Preserve order while removing empty and duplicate URLs."""
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if not u:
            continue
        cleaned = str(u).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


@dataclass
class _FileRecord:
    """Mutable runtime + manifest representation of one downloadable file."""

    raw: dict[str, Any]
    relative_path: str
    name: str
    size: int
    downloaded_bytes: int = 0
    status: str = "queued"
    error: Optional[str] = None
    attempts: int = 0
    sync_reason: Optional[str] = None
    download_url: Optional[str] = None
    fallback_download_urls: list[str] = field(default_factory=list)

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "name": self.name,
            "size": self.size,
            "downloaded_bytes": self.downloaded_bytes,
            "status": self.status,
            "error": self.error,
            "attempts": self.attempts,
            "sync_reason": self.sync_reason,
        }

    def to_manifest_dict(self) -> dict[str, Any]:
        d = dict(self.raw)
        d["name"] = self.name
        d["relative_path"] = self.relative_path
        d["size"] = self.size
        d["download_url"] = self.download_url
        d["fallback_download_urls"] = list(self.fallback_download_urls)
        d["status"] = self.status
        d["downloaded_bytes"] = self.downloaded_bytes
        d["error"] = self.error
        d["attempts"] = self.attempts
        if self.sync_reason is not None:
            d["sync_reason"] = self.sync_reason
        return d


@dataclass
class _WorkerState:
    """Live worker telemetry slot exposed in `get_snapshot()`."""

    worker_id: int
    status: str = "idle"
    file_name: str = ""
    relative_path: str = ""
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_bps: float = 0.0
    attempt: int = 0
    message: str = "في الانتظار"
    _sample_time: float = field(default_factory=time.monotonic)
    _sample_bytes: int = 0

    def reset_idle(self, message: str = "في الانتظار") -> None:
        self.status = "idle"
        self.file_name = ""
        self.relative_path = ""
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.speed_bps = 0.0
        self.attempt = 0
        self.message = message
        self._sample_time = time.monotonic()
        self._sample_bytes = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "status": self.status,
            "file_name": self.file_name,
            "relative_path": self.relative_path,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed_bps": round(self.speed_bps, 2),
            "attempt": self.attempt,
            "message": self.message,
        }


class DownloadManager:
    """Async multi-worker SharePoint file-by-file download manager with resume, retry, and SSE updates."""

    def __init__(self, sp_client: SharePointClient) -> None:
        self.sp_client = sp_client

        self._job_id: str = ""
        self._mode: str = "download"
        self._status: str = "idle"
        self._dest_dir: str = ""
        self._concurrency: int = 3
        self._max_retries: int = 10
        self._skip_verified: bool = True
        self._create_empty_folders: bool = True

        self._folders: list[str] = []
        self._files: list[_FileRecord] = []
        self._workers: list[_WorkerState] = []

        self._queue: asyncio.Queue[Optional[_FileRecord]] = asyncio.Queue()
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._supervisor_task: Optional[asyncio.Task[None]] = None

        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._cancel_event = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._last_session_refresh_at: float = 0.0

        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._last_notify_at: float = 0.0
        self._last_manifest_save_at: float = 0.0
        self._created_at: float = 0.0

        self._speed_samples: deque[tuple[float, int]] = deque()
        self._run_window_start: float = 0.0

    # ------------------------------------------------------------------
    # SSE Subscriptions
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register an SSE subscriber queue and enqueue the current snapshot immediately."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._subscribers.add(q)
        try:
            q.put_nowait(self.get_snapshot())
        except asyncio.QueueFull:
            pass
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove an SSE subscriber queue."""
        self._subscribers.discard(q)

    def _notify_subscribers(self, force: bool = False) -> None:
        if not self._subscribers:
            return
        now = time.monotonic()
        if not force and (now - self._last_notify_at) < 0.25:
            return
        self._last_notify_at = now
        snap = self.get_snapshot()
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    # Public Job Lifecycle API
    # ------------------------------------------------------------------

    async def start_job(
        self,
        dest_dir: str,
        manifest: dict[str, Any],
        concurrency: int = 3,
        max_retries: int = 10,
        skip_verified: bool = True,
        create_empty_folders: bool = True,
        mode: str = "download",
    ) -> dict[str, Any]:
        """Start a new file-by-file download job in the background and return the initial snapshot."""
        if self._status in ("running", "paused") or (
            self._supervisor_task and not self._supervisor_task.done()
        ):
            await self.cancel()

        resolved_dest = str(Path(dest_dir).resolve())
        self._job_id = uuid.uuid4().hex[:12]
        self._mode = mode or "download"
        self._dest_dir = resolved_dest
        self._concurrency = max(1, min(16, int(concurrency)))
        self._max_retries = max(0, int(max_retries))
        self._skip_verified = bool(skip_verified)
        self._create_empty_folders = bool(create_empty_folders)
        self._created_at = time.time()

        raw_folders = list(manifest.get("folders") or [])
        self._folders = list(dict.fromkeys(str(f).strip("/\\") for f in raw_folders if str(f).strip("/\\")))

        # Deduplicate files by normalized relative_path while preserving manifest order
        records: list[_FileRecord] = []
        seen_paths: set[str] = set()
        for raw_item in manifest.get("files") or []:
            item_dict = dict(raw_item)
            name = str(item_dict.get("name") or "unnamed")
            rel = str(item_dict.get("relative_path") or name).replace("\\", "/").strip("/")
            if not rel:
                rel = name
            if rel in seen_paths:
                continue
            seen_paths.add(rel)

            raw_size = item_dict.get("size", -1)
            try:
                size = int(raw_size) if raw_size is not None else -1
            except (TypeError, ValueError):
                size = -1

            records.append(
                _FileRecord(
                    raw=item_dict,
                    relative_path=rel,
                    name=name,
                    size=size,
                    downloaded_bytes=0,
                    status="queued",
                    error=None,
                    attempts=0,
                    sync_reason=item_dict.get("sync_reason"),
                    download_url=item_dict.get("download_url"),
                    fallback_download_urls=list(item_dict.get("fallback_download_urls") or []),
                )
            )

        self._files = records
        self._workers = [_WorkerState(worker_id=i + 1) for i in range(self._concurrency)]
        self._queue = asyncio.Queue()
        self._speed_samples.clear()
        self._run_window_start = time.monotonic()

        self._cancel_event.clear()
        self._pause_event.set()

        # Prepare destination directory and empty folders via build_local_path + win_long_path
        await asyncio.to_thread(self._prepare_directories)

        if not self._files:
            self._status = "completed"
            self._save_manifest(force=True)
            self._notify_subscribers(force=True)
            return self.get_snapshot()

        for rec in self._files:
            self._queue.put_nowait(rec)

        self._status = "running"
        self._save_manifest(force=True)
        self._launch_workers()
        self._notify_subscribers(force=True)
        return self.get_snapshot()

    async def pause(self) -> None:
        """Pause active download workers cleanly without discarding partial `.part` progress."""
        if self._status != "running":
            return
        self._status = "paused"
        self._pause_event.clear()
        for w in self._workers:
            if w.status in ("downloading", "retrying"):
                w.status = "paused"
                w.speed_bps = 0.0
                w.message = "متوقف مؤقتاً"
        self._speed_samples.clear()
        self._save_manifest(force=True)
        self._notify_subscribers(force=True)

    async def resume(self) -> None:
        """Resume a paused download job."""
        if self._status != "paused":
            return
        self._status = "running"
        self._speed_samples.clear()
        self._run_window_start = time.monotonic()
        self._pause_event.set()
        self._save_manifest(force=True)
        self._notify_subscribers(force=True)

    async def cancel(self) -> None:
        """Cancel the current download job, preserving `.part` files on disk for later resume."""
        if self._status not in ("running", "paused") and not (
            self._supervisor_task and not self._supervisor_task.done()
        ):
            return

        self._status = "cancelled"
        self._cancel_event.set()
        self._pause_event.set()  # Unblock any paused workers immediately

        for t in self._worker_tasks:
            if not t.done():
                t.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()

        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass
        self._supervisor_task = None

        # Drain remaining queue items cleanly
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

        for rec in self._files:
            if rec.status == "downloading":
                rec.status = "queued"

        for w in self._workers:
            w.reset_idle("ملغي")

        self._speed_samples.clear()
        self._save_manifest(force=True)
        self._notify_subscribers(force=True)

    async def retry_failed(self) -> None:
        """Re-queue all failed files and restart workers if the job had already finished."""
        failed_records = [rec for rec in self._files if rec.status == "failed"]
        if not failed_records:
            return

        for rec in failed_records:
            rec.status = "queued"
            rec.error = None
            rec.attempts = 0
            self._queue.put_nowait(rec)

        if self._status in ("completed", "cancelled", "idle"):
            self._cancel_event.clear()
            self._pause_event.set()
            self._status = "running"
            self._speed_samples.clear()
            self._run_window_start = time.monotonic()
            for w in self._workers:
                w.reset_idle()
            self._launch_workers()

        self._save_manifest(force=True)
        self._notify_subscribers(force=True)

    def get_snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable live snapshot of job progress, workers, and files."""
        total_files = len(self._files)
        completed_files = 0
        skipped_files = 0
        failed_files = 0
        total_bytes = 0
        downloaded_bytes = 0

        files_out: list[dict[str, Any]] = []
        for rec in self._files:
            if rec.status == "completed":
                completed_files += 1
            elif rec.status == "skipped":
                skipped_files += 1
            elif rec.status == "failed":
                failed_files += 1

            expected = max(0, rec.size)
            total_bytes += expected
            if rec.size >= 0:
                downloaded_bytes += min(rec.downloaded_bytes, rec.size)
            else:
                downloaded_bytes += max(0, rec.downloaded_bytes)

            files_out.append(rec.to_status_dict())

        overall_speed = self._compute_overall_speed()
        eta_seconds: Optional[float] = None
        if self._status == "running" and overall_speed > 1.0:
            remaining = max(0, total_bytes - downloaded_bytes)
            eta_seconds = round(remaining / overall_speed, 1)

        return {
            "job_id": self._job_id,
            "mode": self._mode,
            "status": self._status,
            "dest_dir": self._dest_dir,
            "total_files": total_files,
            "completed_files": completed_files,
            "skipped_files": skipped_files,
            "failed_files": failed_files,
            "total_bytes": total_bytes,
            "downloaded_bytes": downloaded_bytes,
            "overall_speed_bps": round(overall_speed, 2),
            "eta_seconds": eta_seconds,
            "workers": [w.to_dict() for w in self._workers],
            "files": files_out,
        }

    # ------------------------------------------------------------------
    # Internal Directory & Manifest Helpers
    # ------------------------------------------------------------------

    def _prepare_directories(self) -> None:
        os.makedirs(win_long_path(Path(self._dest_dir)), exist_ok=True)
        if self._create_empty_folders:
            for rel_folder in self._folders:
                folder_path = build_local_path(self._dest_dir, rel_folder)
                os.makedirs(win_long_path(folder_path), exist_ok=True)

    def _save_manifest(self, force: bool = False) -> None:
        if not self._dest_dir:
            return
        now = time.monotonic()
        if not force and (now - self._last_manifest_save_at) < 2.0:
            return
        self._last_manifest_save_at = now

        manifest_path = Path(self._dest_dir) / MANIFEST_FILENAME
        tmp_path = Path(str(manifest_path) + ".tmp")
        lp_manifest = win_long_path(manifest_path)
        lp_tmp = win_long_path(tmp_path)

        completed = sum(1 for f in self._files if f.status == "completed")
        skipped = sum(1 for f in self._files if f.status == "skipped")
        failed = sum(1 for f in self._files if f.status == "failed")

        payload = {
            "job_id": self._job_id,
            "mode": self._mode,
            "status": self._status,
            "dest_dir": self._dest_dir,
            "created_at": self._created_at,
            "updated_at": time.time(),
            "concurrency": self._concurrency,
            "max_retries": self._max_retries,
            "skip_verified": self._skip_verified,
            "create_empty_folders": self._create_empty_folders,
            "summary": {
                "total_files": len(self._files),
                "completed_files": completed,
                "skipped_files": skipped,
                "failed_files": failed,
            },
            "folders": list(self._folders),
            "files": [rec.to_manifest_dict() for rec in self._files],
        }
        try:
            os.makedirs(win_long_path(Path(self._dest_dir)), exist_ok=True)
            data = json.dumps(payload, ensure_ascii=False, indent=2)
            with open(lp_tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(lp_tmp, lp_manifest)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Speed Tracking
    # ------------------------------------------------------------------

    def _record_network_bytes(self, num_bytes: int) -> None:
        if num_bytes <= 0:
            return
        now = time.monotonic()
        if self._speed_samples and (now - self._speed_samples[-1][0]) < 0.1:
            ts, prev = self._speed_samples[-1]
            self._speed_samples[-1] = (ts, prev + num_bytes)
        else:
            self._speed_samples.append((now, num_bytes))
        cutoff = now - 5.0
        while self._speed_samples and self._speed_samples[0][0] < cutoff:
            self._speed_samples.popleft()

    def _compute_overall_speed(self) -> float:
        if self._status != "running" or not self._speed_samples:
            return 0.0
        now = time.monotonic()
        cutoff = now - 5.0
        while self._speed_samples and self._speed_samples[0][0] < cutoff:
            self._speed_samples.popleft()
        if not self._speed_samples:
            return 0.0
        window_bytes = sum(b for _, b in self._speed_samples)
        elapsed_since_start = max(0.5, now - self._run_window_start)
        span = min(5.0, elapsed_since_start)
        return window_bytes / span

    # ------------------------------------------------------------------
    # Worker Pool Orchestration
    # ------------------------------------------------------------------

    def _launch_workers(self) -> None:
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(w), name=f"sp-dl-worker-{w.worker_id}")
            for w in self._workers
        ]
        self._supervisor_task = asyncio.create_task(
            self._supervise_job(), name="sp-dl-supervisor"
        )

    async def _supervise_job(self) -> None:
        try:
            await self._queue.join()
            if not self._cancel_event.is_set():
                # Send stop sentinels to all idle workers
                for _ in self._workers:
                    self._queue.put_nowait(None)
                if self._worker_tasks:
                    await asyncio.gather(*self._worker_tasks, return_exceptions=True)
                self._worker_tasks.clear()

                if not self._cancel_event.is_set():
                    self._status = "completed"
                    for w in self._workers:
                        w.reset_idle("مكتمل")
                    self._speed_samples.clear()
                    self._save_manifest(force=True)
                    self._notify_subscribers(force=True)
        except asyncio.CancelledError:
            raise

    async def _worker_loop(self, worker: _WorkerState) -> None:
        client_holder: list[Any] = [None, None]
        try:
            while not self._cancel_event.is_set():
                await self._wait_if_paused(worker, None)
                if self._cancel_event.is_set():
                    break

                rec = await self._queue.get()
                try:
                    if rec is None or self._cancel_event.is_set():
                        if rec is None:
                            break
                        continue

                    client, client_sig = await self._ensure_client(client_holder[0], client_holder[1])
                    client_holder[0], client_holder[1] = client, client_sig
                    await self._download_record(worker, rec, client_holder=client_holder)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if rec is not None and rec.status not in ("completed", "skipped", "failed"):
                        rec.status = "failed"
                        rec.error = str(exc)
                finally:
                    self._queue.task_done()
                    worker.reset_idle()
                    self._save_manifest(force=False)
                    self._notify_subscribers(force=True)
        except asyncio.CancelledError:
            pass
        finally:
            if client_holder[0] is not None:
                try:
                    await client_holder[0].aclose()
                except Exception:
                    pass

    def _session_signature(self) -> Any:
        s = self.sp_client.auth.session
        cookie_sig = tuple(
            (c.get("name"), c.get("value"), c.get("domain"), c.get("path"))
            for c in (s.cookies or [])
        )
        return (s.updated_at, s.authorization_header, s.user_agent, cookie_sig)

    async def _ensure_client(
        self, client: Optional[httpx.AsyncClient], prev_sig: Any
    ) -> tuple[httpx.AsyncClient, Any]:
        sig = self._session_signature()
        if client is not None and sig == prev_sig:
            return client, sig
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        new_client = httpx.AsyncClient(
            cookies=self.sp_client.auth.session.to_httpx_cookies(),
            timeout=httpx.Timeout(connect=25.0, read=60.0, write=30.0, pool=20.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        return new_client, sig

    async def _wait_if_paused(self, worker: _WorkerState, rec: Optional[_FileRecord]) -> None:
        if self._pause_event.is_set():
            return
        worker.status = "paused"
        worker.speed_bps = 0.0
        worker.message = "متوقف مؤقتاً"
        self._notify_subscribers(force=True)
        await self._pause_event.wait()
        if not self._cancel_event.is_set() and rec is not None:
            worker.status = "downloading"
            worker.message = "جاري استئناف التحميل..."

    async def _interruptible_sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._cancel_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _build_download_headers(self, url: str, offset: int) -> dict[str, str]:
        s = self.sp_client.auth.session
        headers: dict[str, str] = {
            "User-Agent": s.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if s.origin:
            headers["Referer"] = s.resolved_url or s.original_url or s.origin
        if s.authorization_header and "/_api/v2.0/" in url:
            headers["Authorization"] = s.authorization_header
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"
        return headers

    async def _refresh_file_urls(self, rec: _FileRecord) -> list[str]:
        try:
            fresh_url, fallbacks = await self.sp_client.refresh_download_url(rec.to_manifest_dict())
            if fresh_url:
                rec.download_url = fresh_url
            if fallbacks:
                rec.fallback_download_urls = list(fallbacks)
        except Exception:
            pass
        return _dedupe_urls(
            ([rec.download_url] if rec.download_url else []) + list(rec.fallback_download_urls or [])
        )

    async def _maybe_refresh_session(self) -> bool:
        async with self._refresh_lock:
            now = time.monotonic()
            if (now - self._last_session_refresh_at) < 5.0:
                return self.sp_client.auth.session.authenticated
            try:
                ok = await self.sp_client.auth.refresh_session_headless()
                self._last_session_refresh_at = time.monotonic()
                return bool(ok)
            except Exception:
                return False

    # ------------------------------------------------------------------
    # Single-File Reliable Download Engine
    # ------------------------------------------------------------------

    async def _download_record(
        self,
        worker: _WorkerState,
        rec: _FileRecord,
        client_holder: list[Any],
    ) -> None:
        final_path = build_local_path(self._dest_dir, rec.relative_path)
        part_path = Path(str(final_path) + ".part")
        lp_final = win_long_path(final_path)
        lp_part = win_long_path(part_path)

        # 1. Skip already verified files when skip_verified=True (never skip sync-updated files)
        if (
            self._skip_verified
            and rec.size >= 0
            and rec.sync_reason != "updated"
            and os.path.isfile(lp_final)
        ):
            try:
                if os.path.getsize(lp_final) == rec.size:
                    if os.path.isfile(lp_part):
                        try:
                            os.remove(lp_part)
                        except OSError:
                            pass
                    rec.status = "skipped"
                    rec.downloaded_bytes = rec.size
                    rec.error = None
                    return
            except OSError:
                pass

        # Ensure parent directory exists
        os.makedirs(win_long_path(final_path.parent), exist_ok=True)

        rec.status = "downloading"
        rec.error = None
        worker.status = "downloading"
        worker.file_name = rec.name
        worker.relative_path = rec.relative_path
        worker.total_bytes = max(0, rec.size)
        worker.speed_bps = 0.0
        worker.message = "جاري تجهيز التحميل..."
        self._notify_subscribers(force=True)

        urls = _dedupe_urls(
            ([rec.download_url] if rec.download_url else []) + list(rec.fallback_download_urls or [])
        )
        if not urls:
            urls = await self._refresh_file_urls(rec)
        if not urls:
            rec.status = "failed"
            rec.error = "لا يوجد رابط تحميل متاح للملف"
            return

        url_idx = 0
        retry_idx = 0
        last_error: Optional[str] = None
        refreshed_url_once = False
        refreshed_session_once = False

        while retry_idx <= self._max_retries:
            if self._cancel_event.is_set():
                rec.status = "queued"
                return

            await self._wait_if_paused(worker, rec)
            if self._cancel_event.is_set():
                rec.status = "queued"
                return

            attempt_num = retry_idx + 1
            rec.attempts = attempt_num
            worker.attempt = attempt_num

            offset = 0
            if os.path.isfile(lp_part):
                try:
                    offset = os.path.getsize(lp_part)
                except OSError:
                    offset = 0
            if rec.size >= 0 and offset > rec.size:
                try:
                    os.remove(lp_part)
                except OSError:
                    pass
                offset = 0

            rec.downloaded_bytes = offset
            worker.downloaded_bytes = offset
            worker.total_bytes = max(0, rec.size)
            worker.status = "downloading" if retry_idx == 0 else "retrying"
            worker.message = (
                "جاري التحميل..."
                if retry_idx == 0
                else f"إعادة المحاولة {attempt_num}/{self._max_retries + 1}..."
            )
            self._notify_subscribers(force=True)

            # Refresh client if session cookies/headers changed
            client, sig = await self._ensure_client(client_holder[0], client_holder[1])
            client_holder[0], client_holder[1] = client, sig

            current_url = urls[url_idx % len(urls)]
            headers = self._build_download_headers(current_url, offset)

            try:
                async with client.stream("GET", current_url, headers=headers) as resp:
                    # Detect Microsoft/ADFS login redirect
                    redirect_urls = [str(resp.url)] + [str(h.url) for h in resp.history]
                    if any(_is_login_redirect(u) for u in redirect_urls):
                        last_error = "تم التوجيه إلى صفحة تسجيل الدخول (انتهت الجلسة)"
                        worker.status = "retrying"
                        worker.speed_bps = 0.0
                        worker.message = "توجيه لصفحة الدخول — تحديث الجلسة والرابط..."
                        self._notify_subscribers(force=True)
                        if not refreshed_session_once:
                            refreshed_session_once = True
                            await self._maybe_refresh_session()
                        fresh_urls = await self._refresh_file_urls(rec)
                        if fresh_urls:
                            url_idx = 0 if fresh_urls[0] != current_url else (url_idx + 1)
                            urls = fresh_urls
                        else:
                            url_idx += 1
                        retry_idx += 1
                        await self._interruptible_sleep(self._backoff_delay(retry_idx - 1))
                        continue

                    # Rate limit / temporary service unavailability
                    if resp.status_code in (429, 502, 503, 504):
                        ra = _parse_retry_after(resp.headers.get("Retry-After"))
                        delay = ra if ra is not None else self._backoff_delay(retry_idx)
                        delay = min(120.0, delay)
                        last_error = f"HTTP {resp.status_code} (تقييد مؤقت من الخادم)"
                        worker.status = "retrying"
                        worker.speed_bps = 0.0
                        worker.message = f"انتظار {delay:.1f}ث (HTTP {resp.status_code})..."
                        self._notify_subscribers(force=True)
                        retry_idx += 1
                        await self._interruptible_sleep(delay)
                        continue

                    # Auth / expired tempauth token / forbidden
                    if resp.status_code in (401, 403):
                        last_error = f"HTTP {resp.status_code} (انتهاء الصلاحية أو رفض الوصول)"
                        worker.status = "retrying"
                        worker.speed_bps = 0.0
                        worker.message = f"تحديث رابط التحميل والجلسة (HTTP {resp.status_code})..."
                        self._notify_subscribers(force=True)
                        got_new_primary = False
                        if not refreshed_url_once:
                            refreshed_url_once = True
                            fresh_urls = await self._refresh_file_urls(rec)
                            if fresh_urls:
                                got_new_primary = fresh_urls[0] != current_url
                                urls = fresh_urls
                        if not got_new_primary and not refreshed_session_once:
                            refreshed_session_once = True
                            if await self._maybe_refresh_session():
                                fresh_urls = await self._refresh_file_urls(rec)
                                if fresh_urls:
                                    got_new_primary = fresh_urls[0] != current_url
                                    urls = fresh_urls
                        elif refreshed_url_once and not got_new_primary:
                            fresh_urls = await self._refresh_file_urls(rec)
                            if fresh_urls:
                                urls = fresh_urls
                        if got_new_primary:
                            url_idx = 0
                        else:
                            url_idx += 1
                        retry_idx += 1
                        await self._interruptible_sleep(self._backoff_delay(retry_idx - 1))
                        continue

                    # Range Not Satisfiable (416)
                    if resp.status_code == 416:
                        cr_total = _parse_416_total(resp.headers.get("Content-Range"))
                        if offset > 0 and (
                            (rec.size >= 0 and offset == rec.size and (cr_total is None or cr_total == rec.size))
                            or (rec.size < 0 and cr_total is not None and offset == cr_total)
                        ):
                            if rec.size < 0 and cr_total is not None:
                                rec.size = cr_total
                            os.replace(lp_part, lp_final)
                            rec.status = "completed"
                            rec.downloaded_bytes = offset
                            rec.error = None
                            return
                        # Mismatched .part size -> discard and retry from byte 0
                        if os.path.isfile(lp_part):
                            try:
                                os.remove(lp_part)
                            except OSError:
                                pass
                        rec.downloaded_bytes = 0
                        last_error = "HTTP 416 — إعادة التحميل من البداية"
                        retry_idx += 1
                        await self._interruptible_sleep(0.2)
                        continue

                    if resp.status_code not in (200, 206):
                        last_error = f"HTTP {resp.status_code}"
                        if resp.status_code in (400, 404, 410):
                            fresh_urls = await self._refresh_file_urls(rec)
                            if fresh_urls:
                                urls = fresh_urls
                        url_idx += 1
                        retry_idx += 1
                        await self._interruptible_sleep(self._backoff_delay(retry_idx - 1))
                        continue

                    # Never write HTML error/login pages to disk as non-HTML target files
                    if _is_unexpected_html(rec.name, resp.headers.get("Content-Type")):
                        last_error = "استجابة HTML غير متوقعة بدلاً من الملف"
                        worker.status = "retrying"
                        worker.speed_bps = 0.0
                        worker.message = "رفض صفحة HTML غير متوقعة — تحديث الجلسة..."
                        self._notify_subscribers(force=True)
                        if not refreshed_session_once:
                            refreshed_session_once = True
                            await self._maybe_refresh_session()
                        fresh_urls = await self._refresh_file_urls(rec)
                        if fresh_urls:
                            url_idx = 0 if fresh_urls[0] != current_url else (url_idx + 1)
                            urls = fresh_urls
                        else:
                            url_idx += 1
                        retry_idx += 1
                        await self._interruptible_sleep(self._backoff_delay(retry_idx - 1))
                        continue

                    content_encoding = (resp.headers.get("Content-Encoding") or "").strip().lower()
                    is_uncompressed = content_encoding in ("", "identity")
                    raw_cl = resp.headers.get("Content-Length")
                    content_length: Optional[int] = (
                        int(raw_cl.strip()) if raw_cl and raw_cl.strip().isdigit() else None
                    )

                    cr_total: Optional[int] = None
                    if resp.status_code == 206:
                        if offset <= 0:
                            last_error = "استجابة 206 غير متوقعة بدون إزاحة"
                            retry_idx += 1
                            continue
                        cr = _parse_content_range(resp.headers.get("Content-Range"))
                        if cr is None or cr[0] != offset:
                            if os.path.isfile(lp_part):
                                try:
                                    os.remove(lp_part)
                                except OSError:
                                    pass
                            rec.downloaded_bytes = 0
                            last_error = "ترويسة Content-Range غير صالحة عند الاستئناف"
                            retry_idx += 1
                            continue
                        cr_start, cr_end, cr_total = cr
                        if cr_total is not None:
                            if rec.size >= 0 and cr_total != rec.size:
                                if os.path.isfile(lp_part):
                                    try:
                                        os.remove(lp_part)
                                    except OSError:
                                        pass
                                rec.downloaded_bytes = 0
                                last_error = f"تغير حجم الملف على الخادم ({cr_total} != {rec.size})"
                                retry_idx += 1
                                continue
                            if rec.size < 0:
                                rec.size = cr_total
                                worker.total_bytes = cr_total
                        open_mode = "ab"
                        base_offset = offset
                    else:
                        # 200 OK -> clean full write from byte 0 (even if Range was sent)
                        if is_uncompressed and content_length is not None:
                            if rec.size >= 0 and content_length != rec.size:
                                last_error = (
                                    f"تعارض Content-Length ({content_length}) مع الحجم المتوقع ({rec.size})"
                                )
                                retry_idx += 1
                                url_idx += 1
                                await self._interruptible_sleep(self._backoff_delay(retry_idx - 1))
                                continue
                            if rec.size < 0:
                                rec.size = content_length
                                worker.total_bytes = content_length
                        open_mode = "wb"
                        base_offset = 0
                        rec.downloaded_bytes = 0
                        worker.downloaded_bytes = 0

                    # Stream response body into .part
                    bytes_this_stream = 0
                    worker._sample_time = time.monotonic()
                    worker._sample_bytes = 0

                    with open(lp_part, open_mode) as fh:
                        try:
                            # Use chunk_size=None so network chunks flush to .part immediately
                            # instead of buffering up to a large block and losing progress on drops.
                            async for chunk in resp.aiter_bytes():
                                if self._cancel_event.is_set():
                                    raise _JobCancelledError()
                                if not self._pause_event.is_set():
                                    raise _JobPausedError()
                                if not chunk:
                                    continue

                                fh.write(chunk)
                                n = len(chunk)
                                bytes_this_stream += n
                                total_written = base_offset + bytes_this_stream
                                rec.downloaded_bytes = total_written
                                worker.downloaded_bytes = total_written
                                self._record_network_bytes(n)

                                now = time.monotonic()
                                worker._sample_bytes += n
                                dt = now - worker._sample_time
                                if dt >= 0.35:
                                    inst = worker._sample_bytes / dt
                                    worker.speed_bps = (
                                        inst if worker.speed_bps <= 0 else (0.4 * inst + 0.6 * worker.speed_bps)
                                    )
                                    worker._sample_time = now
                                    worker._sample_bytes = 0

                                self._notify_subscribers(force=False)
                        finally:
                            fh.flush()

                    # Verify downloaded byte counts before atomic rename
                    total_written = base_offset + bytes_this_stream
                    if is_uncompressed and content_length is not None and bytes_this_stream != content_length:
                        last_error = f"نقص في حجم الاستجابة ({bytes_this_stream} != {content_length})"
                        if bytes_this_stream > content_length:
                            try:
                                os.remove(lp_part)
                            except OSError:
                                pass
                            rec.downloaded_bytes = 0
                        retry_idx += 1
                        await self._interruptible_sleep(self._backoff_delay(retry_idx - 1))
                        continue

                    if rec.size >= 0 and total_written != rec.size:
                        last_error = f"حجم الملف المكتوب لا يطابق الحجم المتوقع ({total_written} != {rec.size})"
                        if total_written > rec.size:
                            try:
                                os.remove(lp_part)
                            except OSError:
                                pass
                            rec.downloaded_bytes = 0
                        retry_idx += 1
                        await self._interruptible_sleep(self._backoff_delay(retry_idx - 1))
                        continue

                    if cr_total is not None and total_written != cr_total:
                        last_error = f"حجم الملف لا يطابق Content-Range ({total_written} != {cr_total})"
                        if total_written > cr_total:
                            try:
                                os.remove(lp_part)
                            except OSError:
                                pass
                            rec.downloaded_bytes = 0
                        retry_idx += 1
                        await self._interruptible_sleep(self._backoff_delay(retry_idx - 1))
                        continue

                    # Atomically finalize .part -> final target path
                    os.replace(lp_part, lp_final)
                    rec.status = "completed"
                    rec.downloaded_bytes = total_written
                    rec.error = None
                    return

            except _JobCancelledError:
                rec.status = "queued"
                return
            except _JobPausedError:
                # Pause cleanly without consuming retry attempts; loop will wait and resume via Range
                continue
            except (httpx.RequestError, httpx.StreamError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                worker.status = "retrying"
                worker.speed_bps = 0.0
                worker.message = f"انقطاع الاتصال — إعادة المحاولة ({attempt_num})..."
                self._notify_subscribers(force=True)
                retry_idx += 1
                await self._interruptible_sleep(self._backoff_delay(retry_idx - 1))
                continue

        rec.status = "failed"
        rec.error = last_error or "فشل التحميل بعد استنفاد جميع المحاولات"

    @staticmethod
    def _backoff_delay(retry_index: int) -> float:
        base = min(60.0, 0.8 * (2 ** min(retry_index, 6)))
        return base + random.uniform(0.1, 0.5)
