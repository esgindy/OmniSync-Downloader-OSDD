"""Utility helpers for SharePoint paths, URLs, Windows long paths, and formatting."""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

# Characters strictly forbidden in Windows NTFS path components
_WIN_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Reserved Windows device names (case-insensitive)
_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def encode_sharing_url(url: str) -> str:
    """Encode a SharePoint/OneDrive sharing URL into a v2.0 shareId (`u!...`)."""
    raw = url.strip().encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"u!{b64}"


def sanitize_windows_component(name: str) -> str:
    """
    Preserve exact SharePoint name whenever valid on Windows NTFS.
    Only replace characters/names strictly illegal on Windows.
    """
    if not name:
        return "_empty_"
    cleaned = _WIN_FORBIDDEN_CHARS.sub("_", name)
    # Windows disallows trailing dots and trailing spaces on path components
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        cleaned = "_"
    stem, dot, ext = cleaned.partition(".")
    if stem.upper() in _WIN_RESERVED_NAMES:
        cleaned = f"_{stem}{dot}{ext}"
    return cleaned


def build_local_path(base_dir: str | Path, relative_path: str) -> Path:
    """
    Join base_dir and a SharePoint-style forward-slash relative_path cleanly,
    preserving exact component names while guarding against path traversal and
    Windows-forbidden characters.
    """
    base = Path(base_dir).resolve()
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p and p not in (".", "..")]
    safe_parts = [sanitize_windows_component(p) for p in parts]
    target = base.joinpath(*safe_parts) if safe_parts else base
    return target


def win_long_path(path: str | Path) -> str:
    """
    Convert an absolute Windows path to an extended-length `\\\\?\\` path when on Windows
    so paths exceeding 260 characters (common in nested project folders) work reliably.
    """
    raw = str(path)
    if os.name == "nt" and raw.startswith("\\\\?\\"):
        return raw
    p = str(Path(path).resolve())
    if os.name != "nt":
        return p
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        # UNC path \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + p.lstrip("\\")
    return "\\\\?\\" + p


def escape_odata_single_quotes(value: str) -> str:
    """Escape single quotes for SharePoint REST OData parameter literals."""
    return value.replace("'", "''")


def encode_sp_path_param(server_relative_path: str) -> str:
    """
    Percent-encode a SharePoint server-relative path for `@p='...'` REST calls.
    Handles `#`, `%`, `&`, spaces, single quotes, and UTF-8 Arabic characters.
    """
    escaped = escape_odata_single_quotes(server_relative_path)
    return quote(escaped, safe="/:")


def extract_sharepoint_url_hints(url: str) -> dict[str, Optional[str]]:
    """
    Parse a SharePoint URL (direct library, AllItems.aspx, onedrive.aspx, or sharing link)
    for origin, probable web URL, and `id` / `RootFolder` query parameter.
    """
    parsed = urlparse(url.strip())
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    qs = parse_qs(parsed.query)

    folder_id = None
    for key in ("id", "RootFolder", "rootfolder"):
        if key in qs and qs[key]:
            folder_id = unquote(qs[key][0])
            break

    path_parts = [p for p in parsed.path.split("/") if p]
    web_path = ""
    # Typical SharePoint managed paths: /sites/<name>, /teams/<name>, /personal/<user>
    if len(path_parts) >= 2 and path_parts[0].lower() in ("sites", "teams", "personal"):
        web_path = "/" + "/".join(path_parts[:2])
    elif len(path_parts) >= 3 and path_parts[0].startswith(":") and path_parts[0].endswith(":"):
        scope = path_parts[1].lower()
        # /:f:/s/<site>/<token>, /:f:/t/<team>/<token>, /:f:/g/personal/<user>/<token>, /:f:/r/sites/<site>/...
        if scope == "s":
            web_path = f"/sites/{path_parts[2]}"
        elif scope == "t":
            web_path = f"/teams/{path_parts[2]}"
        elif scope == "g" and path_parts[2].lower() == "personal" and len(path_parts) >= 4:
            web_path = f"/personal/{path_parts[3]}"
        elif scope == "r" and len(path_parts) >= 4 and path_parts[2].lower() in ("sites", "teams", "personal"):
            web_path = "/" + "/".join(path_parts[2:4])

    web_url = f"{origin}{web_path}" if origin else ""
    is_sharing_link = bool(
        len(path_parts) >= 1 and path_parts[0].startswith(":") and path_parts[0].endswith(":")
    ) or "guestaccess.aspx" in parsed.path.lower()

    return {
        "origin": origin,
        "web_url": web_url,
        "web_path": web_path,
        "folder_id": folder_id,
        "is_sharing_link": "1" if is_sharing_link else "",
    }


def format_bytes(num_bytes: int) -> str:
    """Human-readable file size string."""
    if num_bytes < 0:
        return "غير معروف"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"
