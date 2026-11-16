"""Universal Media & Social Downloader Engine (YouTube, TikTok, Instagram, Facebook, X, etc.)
Part of OmniSync-Downloader (OSDD) v4.0.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import yt_dlp

from utils import win_long_path


def format_duration(seconds: Optional[int | float]) -> str:
    """Format duration in seconds to mm:ss or hh:mm:ss."""
    if seconds is None or seconds <= 0:
        return "--:--"
    total_sec = int(seconds)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def sanitize_filename(name: str) -> str:
    """Remove illegal filesystem characters."""
    if not name:
        return "unnamed_media"
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return cleaned[:180] if len(cleaned) > 180 else cleaned


def detect_media_platform(url: str) -> dict[str, str]:
    """Detect platform from URL (YouTube, TikTok, Instagram, Twitter/X, Facebook, etc.)."""
    if not url:
        return {"id": "unknown", "name": "Unknown", "icon": "fa-globe"}

    parsed = urlparse(url.strip())
    domain = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()

    if any(d in domain for d in ["youtube.com", "youtu.be"]):
        if "playlist" in path or "list=" in (parsed.query or ""):
            return {"id": "youtube_playlist", "name": "YouTube Playlist", "icon": "fa-list"}
        if "shorts" in path:
            return {"id": "youtube_shorts", "name": "YouTube Shorts", "icon": "fa-play-circle"}
        return {"id": "youtube", "name": "YouTube", "icon": "fa-brands fa-youtube"}

    if "tiktok.com" in domain:
        return {"id": "tiktok", "name": "TikTok", "icon": "fa-brands fa-tiktok"}

    if "instagram.com" in domain:
        return {"id": "instagram", "name": "Instagram", "icon": "fa-brands fa-instagram"}

    if any(d in domain for d in ["twitter.com", "x.com"]):
        return {"id": "twitter", "name": "X (Twitter)", "icon": "fa-brands fa-x-twitter"}

    if any(d in domain for d in ["facebook.com", "fb.watch", "fb.com"]):
        return {"id": "facebook", "name": "Facebook", "icon": "fa-brands fa-facebook"}

    if "reddit.com" in domain or "redd.it" in domain:
        return {"id": "reddit", "name": "Reddit", "icon": "fa-brands fa-reddit"}

    if "vimeo.com" in domain:
        return {"id": "vimeo", "name": "Vimeo", "icon": "fa-brands fa-vimeo"}

    if "soundcloud.com" in domain:
        return {"id": "soundcloud", "name": "SoundCloud", "icon": "fa-brands fa-soundcloud"}

    if "pinterest.com" in domain or "pin.it" in domain:
        return {"id": "pinterest", "name": "Pinterest", "icon": "fa-brands fa-pinterest"}

    return {"id": "universal", "name": "Universal Video/Audio", "icon": "fa-film"}


class CancelledDownloadError(Exception):
    """Raised when a download is cancelled by the user."""
    pass


class MediaDownloadManager:
    """Manages universal media info extraction and streaming downloads with live telemetry."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._cancel_event = threading.Event()
        self._current_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._job_id: str = ""
        self._status: str = "idle"  # idle, running, completed, cancelled, failed
        self._url: str = ""
        self._title: str = ""
        self._thumbnail: str = ""
        self._dest_dir: str = ""
        self._platform: dict[str, str] = {"id": "unknown", "name": "Unknown", "icon": "fa-globe"}
        self._is_playlist: bool = False
        self._total_files: int = 0
        self._completed_files: int = 0
        self._failed_files: int = 0
        self._total_bytes: int = 0
        self._downloaded_bytes: int = 0
        self._overall_speed_bps: float = 0.0
        self._eta_seconds: Optional[float] = None
        self._current_file: str = ""
        self._error: Optional[str] = None
        self._files: list[dict[str, Any]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self._lock:
            self._subscribers.add(q)
            q.put_nowait(self.get_snapshot())
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def _notify(self) -> None:
        snap = self.get_snapshot()
        with self._lock:
            dead: list[asyncio.Queue[dict[str, Any]]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(snap)
                except Exception:
                    dead.append(q)
            for d in dead:
                self._subscribers.discard(d)

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            remaining_files = max(0, self._total_files - self._completed_files)
            remaining_bytes = max(0, self._total_bytes - self._downloaded_bytes)
            return {
                "job_id": self._job_id,
                "mode": "media",
                "status": self._status,
                "url": self._url,
                "title": self._title,
                "thumbnail": self._thumbnail,
                "dest_dir": self._dest_dir,
                "platform": self._platform,
                "is_playlist": self._is_playlist,
                "total_files": self._total_files,
                "completed_files": self._completed_files,
                "remaining_files": remaining_files,
                "failed_files": self._failed_files,
                "total_bytes": self._total_bytes,
                "downloaded_bytes": self._downloaded_bytes,
                "remaining_bytes": remaining_bytes,
                "overall_speed_bps": round(self._overall_speed_bps, 2),
                "eta_seconds": self._eta_seconds,
                "current_file": self._current_file,
                "error": self._error,
                "files": list(self._files),
            }

    async def extract_info(self, url: str) -> dict[str, Any]:
        """Extract metadata, formats, and playlist info without downloading."""
        platform = detect_media_platform(url)

        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
            "noplaylist": False,
        }

        def _sync_extract() -> dict[str, Any]:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url.strip(), download=False) or {}

        loop = asyncio.get_running_loop()
        raw_info = await loop.run_in_executor(None, _sync_extract)

        is_playlist = raw_info.get("_type") == "playlist" or "entries" in raw_info
        entries = []
        if is_playlist:
            raw_entries = raw_info.get("entries") or []
            for i, ent in enumerate(raw_entries, start=1):
                if not ent:
                    continue
                entries.append({
                    "index": i,
                    "id": ent.get("id") or str(i),
                    "title": ent.get("title") or f"Item {i}",
                    "duration": ent.get("duration"),
                    "duration_str": format_duration(ent.get("duration")),
                    "url": ent.get("url") or ent.get("webpage_url") or "",
                })

        duration = raw_info.get("duration")
        thumbnail = raw_info.get("thumbnail") or ""
        if not thumbnail and is_playlist and entries:
            thumbnail = ""

        # Available presets
        preset_formats = [
            {"id": "best", "name": "Best Quality (Video + Audio)", "desc": "Highest available resolution & audio", "ext": "mp4"},
            {"id": "1080p", "name": "Full HD (1080p)", "desc": "1080p video with best audio", "ext": "mp4"},
            {"id": "720p", "name": "HD (720p)", "desc": "720p video with best audio", "ext": "mp4"},
            {"id": "480p", "name": "SD (480p)", "desc": "Standard quality / smaller size", "ext": "mp4"},
            {"id": "audio_mp3", "name": "Audio Only (MP3)", "desc": "Extract and save as MP3", "ext": "mp3"},
            {"id": "audio_m4a", "name": "Audio Only (M4A)", "desc": "High quality AAC / M4A stream", "ext": "m4a"},
        ]

        return {
            "url": url,
            "platform": platform,
            "title": raw_info.get("title") or "Unknown Title",
            "uploader": raw_info.get("uploader") or raw_info.get("channel") or "",
            "duration": duration,
            "duration_str": format_duration(duration),
            "thumbnail": thumbnail,
            "is_playlist": is_playlist,
            "playlist_count": len(entries) if is_playlist else 1,
            "entries": entries,
            "formats": preset_formats,
        }

    def start_download(
        self,
        url: str,
        dest_dir: str,
        format_id: str = "best",
        audio_only: bool = False,
    ) -> dict[str, Any]:
        """Start downloading in a dedicated background worker thread."""
        with self._lock:
            if self._status == "running":
                raise RuntimeError("A download job is already active.")

            self._job_id = f"media_{int(time.time() * 1000)}"
            self._status = "running"
            self._url = url.strip()
            self._dest_dir = dest_dir.strip()
            self._platform = detect_media_platform(self._url)
            self._cancel_event.clear()
            self._error = None
            self._downloaded_bytes = 0
            self._total_bytes = 0
            self._overall_speed_bps = 0.0
            self._eta_seconds = None
            self._current_file = "Initializing..."
            self._completed_files = 0
            self._failed_files = 0
            self._total_files = 1
            self._files = []

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        self._current_thread = threading.Thread(
            target=self._worker_thread,
            args=(self._url, self._dest_dir, format_id, audio_only),
            daemon=True,
        )
        self._current_thread.start()
        return self.get_snapshot()

    def cancel(self) -> None:
        """Signal cancellation to yt-dlp."""
        with self._lock:
            if self._status == "running":
                self._cancel_event.set()
                self._status = "cancelled"
                self._current_file = "Cancelled by user"
        self._notify()

    def _worker_thread(
        self,
        url: str,
        dest_dir: str,
        format_id: str,
        audio_only: bool,
    ) -> None:
        """Background thread executing yt-dlp download with live progress hooks."""
        out_dir = Path(dest_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        lp_out_dir = win_long_path(out_dir)

        # Output template
        outtmpl = os.path.join(lp_out_dir, "%(title)s.%(ext)s")

        # Format string resolution
        fmt_str = "bestvideo+bestaudio/best"
        postprocessors: list[dict[str, Any]] = []

        if format_id == "audio_mp3" or (audio_only and format_id != "audio_m4a"):
            fmt_str = "bestaudio/best"
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            })
        elif format_id == "audio_m4a":
            fmt_str = "bestaudio[ext=m4a]/bestaudio/best"
        elif format_id == "1080p":
            fmt_str = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
        elif format_id == "720p":
            fmt_str = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        elif format_id == "480p":
            fmt_str = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
        else:
            fmt_str = "bestvideo+bestaudio/best"

        def _progress_hook(d: dict[str, Any]) -> None:
            if self._cancel_event.is_set():
                raise CancelledDownloadError("Download stopped by user")

            status = d.get("status")
            if status == "downloading":
                raw_filename = d.get("filename") or ""
                basename = os.path.basename(raw_filename)

                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                speed = d.get("speed") or 0.0
                eta = d.get("eta")

                with self._lock:
                    self._current_file = basename
                    self._downloaded_bytes = downloaded
                    if total > 0:
                        self._total_bytes = total
                    self._overall_speed_bps = float(speed or 0.0)
                    self._eta_seconds = float(eta) if eta is not None else None

                self._notify()

            elif status == "finished":
                raw_filename = d.get("filename") or ""
                basename = os.path.basename(raw_filename)
                with self._lock:
                    self._completed_files += 1
                    self._files.append({
                        "filename": basename,
                        "status": "completed",
                        "size": self._downloaded_bytes,
                    })
                self._notify()

        ydl_opts: dict[str, Any] = {
            "outtmpl": outtmpl,
            "format": fmt_str,
            "progress_hooks": [_progress_hook],
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "windowsfilenames": True,
        }

        if postprocessors:
            ydl_opts["postprocessors"] = postprocessors

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Pre-fetch info to get clean title and count
                info = ydl.extract_info(url, download=False) or {}
                with self._lock:
                    self._title = info.get("title") or "Media Download"
                    self._thumbnail = info.get("thumbnail") or ""
                    if info.get("_type") == "playlist" or "entries" in info:
                        self._is_playlist = True
                        entries = info.get("entries") or []
                        self._total_files = len(entries) if entries else 1
                    else:
                        self._total_files = 1
                self._notify()

                # Execute download
                ydl.download([url])

            with self._lock:
                if self._status != "cancelled":
                    self._status = "completed"
                    self._current_file = "All media downloaded successfully."
                    if self._total_bytes > 0:
                        self._downloaded_bytes = self._total_bytes
                    self._overall_speed_bps = 0.0
                    self._eta_seconds = None
            self._notify()

        except CancelledDownloadError:
            with self._lock:
                self._status = "cancelled"
                self._error = "Download was cancelled."
                self._overall_speed_bps = 0.0
            self._notify()
        except Exception as exc:
            with self._lock:
                self._status = "failed"
                self._error = str(exc)
                self._overall_speed_bps = 0.0
            self._notify()
