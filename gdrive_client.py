"""Google Drive URL parser, folder listing, and direct download link resolver.
Part of OmniSync-Downloader (OSDD) v3.0 Multi-Cloud Streaming Engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional
import httpx

from sp_client import http2_available

# Patterns for extracting Google Drive IDs
GDRIVE_FOLDER_RE = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]+)")
GDRIVE_FILE_RE = re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)")
GDRIVE_OPEN_RE = re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)")
GDRIVE_UC_RE = re.compile(r"drive\.google\.com/uc\?(?:.*&)?id=([a-zA-Z0-9_-]+)")
CONFIRM_TOKEN_RE = re.compile(r"confirm=([0-9A-Za-z_-]+)")
ACTION_CONFIRM_RE = re.compile(r'href="([^"]+confirm=[0-9A-Za-z_-]+[^"]*)"')


def extract_gdrive_id(url: str) -> tuple[Optional[str], str]:
    """
    Extract (item_id, item_type) from a Google Drive URL.
    item_type can be 'folder' or 'file' or 'unknown'.
    """
    if not url:
        return None, "unknown"

    m = GDRIVE_FOLDER_RE.search(url)
    if m:
        return m.group(1), "folder"

    m = GDRIVE_FILE_RE.search(url)
    if m:
        return m.group(1), "file"

    m = GDRIVE_OPEN_RE.search(url)
    if m:
        return m.group(1), "unknown"

    m = GDRIVE_UC_RE.search(url)
    if m:
        return m.group(1), "file"

    cleaned = url.strip()
    if re.match(r"^[a-zA-Z0-9_-]{20,}$", cleaned):
        return cleaned, "unknown"

    return None, "unknown"


def get_gdrive_direct_download_url(file_id: str) -> str:
    """Generate the standard direct download URL for a Google Drive file."""
    return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"


@dataclass
class GDriveItem:
    id: str
    name: str
    size: int
    is_folder: bool
    relative_path: str = ""
    mime_type: str = ""
    md5_checksum: Optional[str] = None
    download_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "is_folder": self.is_folder,
            "relative_path": self.relative_path,
            "mime_type": self.mime_type,
            "md5_checksum": self.md5_checksum,
            "download_url": self.download_url,
        }


class GDriveClient:
    """Google Drive client for scanning folders and resolving direct file streams."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            http2=http2_available(),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
            },
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def resolve_direct_stream(
        self,
        file_id: str,
        custom_headers: Optional[dict[str, str]] = None,
    ) -> tuple[str, dict[str, str]]:
        """
        Resolve a Google Drive file ID to its final direct streaming URL,
        automatically handling the large-file virus scan warning redirect if present.
        """
        headers = dict(custom_headers or {})
        initial_url = get_gdrive_direct_download_url(file_id)

        resp = await self.client.get(initial_url, headers=headers)
        content_type = resp.headers.get("content-type", "").lower()

        # If redirected to virus scan warning page
        if "text/html" in content_type and ("virus" in resp.text.lower() or "download anyway" in resp.text.lower() or "confirm" in resp.text.lower()):
            m = CONFIRM_TOKEN_RE.search(resp.text)
            if m:
                confirm_token = m.group(1)
                confirmed_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm={confirm_token}"
                return confirmed_url, headers
            m2 = ACTION_CONFIRM_RE.search(resp.text)
            if m2:
                raw_href = m2.group(1).replace("&amp;", "&")
                if not raw_href.startswith("http"):
                    raw_href = f"https://drive.google.com{raw_href}"
                return raw_href, headers

        return str(resp.url), headers

    async def scan_folder(
        self,
        folder_id: str,
        folder_name: str = "GDrive_Folder",
        api_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Scan a Google Drive folder recursively and produce a standard manifest
        compatible with DownloadManager and SyncEngine.
        """
        key = api_key or self.api_key
        all_folders: list[str] = []
        all_files: list[dict[str, Any]] = []

        if key:
            # Use Google Drive v3 REST API
            await self._scan_via_api(folder_id, "", all_folders, all_files, key)
        else:
            # Public Web Client scraper for unauthenticated shared folders
            await self._scan_via_web(folder_id, "", all_folders, all_files)

        return {
            "root_id": folder_id,
            "root_name": folder_name,
            "folders": sorted(list(set(all_folders)), key=str.lower),
            "files": all_files,
            "total_files": len(all_files),
            "total_bytes": sum(f.get("size", 0) for f in all_files if f.get("size", 0) > 0),
        }

    async def _scan_via_api(
        self,
        parent_id: str,
        current_rel: str,
        folders_acc: list[str],
        files_acc: list[dict[str, Any]],
        api_key: str,
    ) -> None:
        """Recursively list files & folders using Google Drive v3 REST API."""
        page_token: Optional[str] = None
        url = "https://www.googleapis.com/drive/v3/files"

        while True:
            params: dict[str, Any] = {
                "q": f"'{parent_id}' in parents and trashed = false",
                "fields": "nextPageToken, files(id, name, size, mimeType, md5Checksum)",
                "pageSize": 1000,
                "key": api_key,
            }
            if page_token:
                params["pageToken"] = page_token

            resp = await self.client.get(url, params=params)
            if resp.status_code != 200:
                break

            data = resp.json()
            for item in data.get("files", []):
                item_id = item.get("id")
                name = item.get("name")
                mime = item.get("mimeType", "")
                is_folder = (mime == "application/vnd.google-apps.folder")
                rel = f"{current_rel}/{name}" if current_rel else name

                if is_folder:
                    folders_acc.append(rel)
                    await self._scan_via_api(item_id, rel, folders_acc, files_acc, api_key)
                else:
                    size = int(item.get("size") or 0)
                    files_acc.append({
                        "name": name,
                        "relative_path": rel,
                        "size": size,
                        "drive_id": item_id,
                        "item_id": item_id,
                        "download_url": get_gdrive_direct_download_url(item_id),
                        "md5_checksum": item.get("md5Checksum"),
                        "mime_type": mime,
                        "sync_reason": "new",
                    })

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    async def _scan_via_web(
        self,
        folder_id: str,
        current_rel: str,
        folders_acc: list[str],
        files_acc: list[dict[str, Any]],
    ) -> None:
        """Parse publicly accessible Google Drive shared folder page."""
        url = f"https://drive.google.com/drive/folders/{folder_id}"
        resp = await self.client.get(url)
        text = resp.text

        # Extract items embedded in page data (JSON arrays)
        matches = re.findall(r'\["([a-zA-Z0-9_-]{25,})",\["([^"]+)",(\d+),', text)
        if matches:
            for item_id, name, raw_size in matches:
                try:
                    size = int(raw_size)
                except ValueError:
                    size = 0
                rel = f"{current_rel}/{name}" if current_rel else name
                files_acc.append({
                    "name": name,
                    "relative_path": rel,
                    "size": size,
                    "drive_id": item_id,
                    "item_id": item_id,
                    "download_url": get_gdrive_direct_download_url(item_id),
                    "sync_reason": "new",
                })


class CloudToCloudBridge:
    """
    Direct Cloud-to-Cloud Bridge & Pipeline (v3.0).
    Streams data directly from a source cloud link (SharePoint, OneDrive, Google Drive)
    to a destination cloud or storage endpoint without saving the entire file locally first.
    """

    def __init__(self) -> None:
        self._status: str = "idle"  # idle, running, paused, completed, cancelled, error
        self._source_url: str = ""
        self._target_url: str = ""
        self._source_provider: str = ""
        self._target_provider: str = ""
        self._total_files: int = 0
        self._transferred_files: int = 0
        self._total_bytes: int = 0
        self._transferred_bytes: int = 0
        self._current_file: str = ""
        self._speed_bps: float = 0.0
        self._eta_seconds: Optional[float] = None
        self._error: Optional[str] = None
        self._last_time: float = 0.0
        self._last_bytes: int = 0
        self._log: list[dict[str, Any]] = []

    def get_snapshot(self) -> dict[str, Any]:
        pct = (
            (self._transferred_bytes / self._total_bytes * 100.0)
            if self._total_bytes > 0
            else (self._transferred_files / self._total_files * 100.0 if self._total_files > 0 else 0.0)
        )
        return {
            "status": self._status,
            "source_url": self._source_url,
            "target_url": self._target_url,
            "source_provider": self._source_provider,
            "target_provider": self._target_provider,
            "total_files": self._total_files,
            "transferred_files": self._transferred_files,
            "remaining_files": max(0, self._total_files - self._transferred_files),
            "total_bytes": self._total_bytes,
            "transferred_bytes": self._transferred_bytes,
            "remaining_bytes": max(0, self._total_bytes - self._transferred_bytes),
            "current_file": self._current_file,
            "speed_bps": round(self._speed_bps, 2),
            "eta_seconds": self._eta_seconds,
            "progress_pct": round(min(100.0, pct), 2),
            "error": self._error,
            "recent_files": self._log[-20:],
        }

    def start_pipeline(
        self,
        source_url: str,
        target_url: str,
        source_provider: str,
        target_provider: str,
        files: list[dict[str, Any]],
    ) -> None:
        self._source_url = source_url
        self._target_url = target_url
        self._source_provider = source_provider
        self._target_provider = target_provider
        self._total_files = len(files)
        self._transferred_files = 0
        self._total_bytes = sum(f.get("size", 0) for f in files)
        self._transferred_bytes = 0
        self._current_file = ""
        self._speed_bps = 0.0
        self._eta_seconds = None
        self._status = "running"
        self._error = None
        self._log = []
        self._last_time = time.monotonic()
        self._last_bytes = 0
