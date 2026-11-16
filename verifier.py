"""Post-download verification and CSV/JSON export for SharePoint Downloader."""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils import build_local_path, win_long_path

ROOT_FOLDER_LABEL = "(المجلد الرئيسي)"


def _normalize_rel_path(raw_path: str) -> str:
    return (raw_path or "").replace("\\", "/").strip("/")


def _parent_folder(rel_path: str) -> str:
    if "/" in rel_path:
        return rel_path.rsplit("/", 1)[0]
    return ""


def _parse_expected_size(raw_size: Any) -> int:
    if raw_size is None:
        return -1
    try:
        return int(raw_size)
    except (TypeError, ValueError):
        return -1


def verify_download(manifest: dict[str, Any], dest_dir: str | Path) -> dict[str, Any]:
    """
    Compare every folder in `manifest["folders"]` and every file in `manifest["files"]`
    against `dest_dir` using `build_local_path` and `win_long_path`.
    """
    base_dir = Path(dest_dir).resolve()
    raw_folders = manifest.get("folders") or []
    raw_files = manifest.get("files") or []

    seen_folders: set[str] = set()
    verified_folders = 0
    missing_folder_list: list[str] = []

    for raw_folder in raw_folders:
        rel_folder = _normalize_rel_path(str(raw_folder))
        if not rel_folder or rel_folder in seen_folders:
            continue
        seen_folders.add(rel_folder)
        local_folder = build_local_path(base_dir, rel_folder)
        if os.path.isdir(win_long_path(local_folder)):
            verified_folders += 1
        else:
            missing_folder_list.append(rel_folder)

    total_folders = len(seen_folders)
    missing_folders = len(missing_folder_list)

    items: list[dict[str, Any]] = []
    retry_files: list[dict[str, Any]] = []

    verified_files = 0
    missing_files = 0
    size_mismatch_files = 0
    incomplete_part_files = 0
    total_expected_bytes = 0
    total_verified_bytes = 0

    for entry in raw_files:
        if not isinstance(entry, dict):
            continue
        rel_path = _normalize_rel_path(entry.get("relative_path") or entry.get("name") or "")
        name = entry.get("name") or (rel_path.rsplit("/", 1)[-1] if rel_path else "")
        folder = _parent_folder(rel_path)
        expected_size = _parse_expected_size(entry.get("size"))
        if expected_size > 0:
            total_expected_bytes += expected_size

        local_file = build_local_path(base_dir, rel_path) if rel_path else base_dir / (name or "unnamed")
        part_file = Path(f"{local_file}.part")
        long_final = win_long_path(local_file)
        long_part = win_long_path(part_file)

        file_exists = os.path.isfile(long_final)
        has_part = os.path.isfile(long_part)

        local_size = 0
        if file_exists:
            try:
                local_size = os.stat(long_final).st_size
            except OSError:
                file_exists = False
                local_size = 0

        if has_part:
            status = "incomplete_part"
            incomplete_part_files += 1
        elif not file_exists:
            status = "missing"
            missing_files += 1
        elif expected_size >= 0 and local_size != expected_size:
            status = "size_mismatch"
            size_mismatch_files += 1
        else:
            status = "verified"
            verified_files += 1
            total_verified_bytes += local_size

        items.append(
            {
                "relative_path": rel_path,
                "name": name,
                "folder": folder,
                "expected_size": expected_size,
                "local_size": local_size,
                "status": status,
                "has_part_file": has_part,
            }
        )

        if status != "verified":
            retry_entry = dict(entry)
            if not retry_entry.get("sync_reason"):
                retry_entry["sync_reason"] = "missing"
            retry_files.append(retry_entry)

    total_files = len(items)
    all_ok = (
        missing_folders == 0
        and missing_files == 0
        and size_mismatch_files == 0
        and incomplete_part_files == 0
    )

    retry_manifest = {
        "folders": list(missing_folder_list),
        "files": retry_files,
    }

    return {
        "dest_dir": str(base_dir),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "total_folders": total_folders,
        "verified_folders": verified_folders,
        "missing_folders": missing_folders,
        "total_files": total_files,
        "verified_files": verified_files,
        "missing_files": missing_files,
        "size_mismatch_files": size_mismatch_files,
        "incomplete_part_files": incomplete_part_files,
        "total_expected_bytes": total_expected_bytes,
        "total_verified_bytes": total_verified_bytes,
        "all_ok": all_ok,
        "missing_folder_list": missing_folder_list,
        "items": items,
        "retry_manifest": retry_manifest,
    }


def export_verification_csv(report: dict[str, Any]) -> str:
    """Export verification report items as UTF-8 CSV with BOM for Arabic Excel compatibility."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "relative_path",
            "name",
            "folder",
            "status",
            "expected_size",
            "local_size",
            "has_part_file",
        ]
    )
    for item in report.get("items") or []:
        writer.writerow(
            [
                item.get("relative_path", ""),
                item.get("name", ""),
                item.get("folder") or ROOT_FOLDER_LABEL,
                item.get("status", ""),
                item.get("expected_size", ""),
                item.get("local_size", 0),
                bool(item.get("has_part_file", False)),
            ]
        )
    return "\ufeff" + buf.getvalue()


def export_verification_json(report: dict[str, Any]) -> str:
    """Export verification report as UTF-8 formatted JSON."""
    return json.dumps(report, ensure_ascii=False, indent=2)
