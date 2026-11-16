"""Google Drive URL parser, folder listing, and direct download link resolver."""

from __future__ import annotations

import re
from typing import Any, Optional
import httpx

# Patterns for extracting Google Drive IDs
GDRIVE_FOLDER_RE = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]+)")
GDRIVE_FILE_RE = re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)")
GDRIVE_OPEN_RE = re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)")
GDRIVE_UC_RE = re.compile(r"drive\.google\.com/uc\?(?:.*&)?id=([a-zA-Z0-9_-]+)")

CONFIRM_TOKEN_RE = re.compile(r"confirm=([0-9A-Za-z_-]+)")


def extract_gdrive_id(url: str) -> tuple[Optional[str], str]:
    """
    Extract (item_id, item_type) from a Google Drive URL.
    item_type can be 'folder' or 'file'.
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

    # Bare alphanumeric ID
    cleaned = url.strip()
    if re.match(r"^[a-zA-Z0-9_-]{20,}$", cleaned):
        return cleaned, "unknown"

    return None, "unknown"


def get_gdrive_direct_download_url(file_id: str) -> str:
    """Generate the standard direct download URL for a Google Drive file."""
    return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"


async def resolve_gdrive_download_stream(
    client: httpx.AsyncClient,
    file_id: str,
    headers: Optional[dict[str, str]] = None,
) -> tuple[str, dict[str, str]]:
    """
    Resolve Google Drive direct download URL, handling virus scan confirmation redirects if necessary.
    """
    req_headers = dict(headers or {})
    initial_url = get_gdrive_direct_download_url(file_id)

    resp = await client.get(initial_url, headers=req_headers, follow_redirects=True)
    
    # If returned HTML with virus scan warning prompt, find confirm token
    content_type = resp.headers.get("content-type", "").lower()
    if "text/html" in content_type and "virus" in resp.text.lower():
        m = CONFIRM_TOKEN_RE.search(resp.text)
        if m:
            confirm_token = m.group(1)
            confirmed_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm={confirm_token}"
            return confirmed_url, req_headers

    return str(resp.url), req_headers
