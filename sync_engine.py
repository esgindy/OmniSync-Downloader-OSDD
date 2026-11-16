"""SharePoint <-> Local directory comparison and sync reporting engine."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from utils import build_local_path, sanitize_windows_component, win_long_path

ROOT_FOLDER_LABEL = "(المجلد الرئيسي)"
MANIFEST_FILENAME = ".sp_downloader_manifest.json"
_IS_WINDOWS = os.name == "nt"
_ODATA_DATE_RE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d+)?\)/$")


def _normalize_rel_path(raw_path: str) -> str:
    return (raw_path or "").replace("\\", "/").strip("/")


def _parent_folder_label(rel_path: str) -> str:
    if "/" in rel_path:
        return rel_path.rsplit("/", 1)[0]
    return ROOT_FOLDER_LABEL


def _sanitized_rel_path(rel_path: str) -> str:
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p not in (".", "..")]
    return "/".join(sanitize_windows_component(p) for p in parts)


def _match_key(rel_path: str) -> str:
    return rel_path.lower() if _IS_WINDOWS else rel_path


def _parse_int_size(raw_size: Any) -> int:
    if raw_size is None:
        return -1
    try:
        return int(raw_size)
    except (TypeError, ValueError):
        return -1


def _parse_sp_timestamp(raw_value: Any) -> Optional[float]:
    """Parse SharePoint timestamp strings (ISO 8601, /Date(...)/, or common US/EU formats) to UTC epoch seconds."""
    if not raw_value or not isinstance(raw_value, str):
        return None
    text = raw_value.strip()
    if not text:
        return None

    m = _ODATA_DATE_RE.match(text)
    if m:
        try:
            return int(m.group(1)) / 1000.0
        except ValueError:
            return None

    iso_candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _scan_local_files(local_dir: Path) -> dict[str, dict[str, Any]]:
    """Walk `local_dir` safely using `win_long_path`, returning a map keyed for local/SharePoint matching."""
    long_base = win_long_path(local_dir)
    if not os.path.isdir(long_base):
        return {}

    root_norm = long_base.rstrip("\\/")
    local_files: dict[str, dict[str, Any]] = {}

    for dirpath, dirnames, filenames in os.walk(long_base):
        dirnames.sort()
        filenames.sort()

        cleaned_dir = dirpath.rstrip("\\/")
        if cleaned_dir == root_norm:
            rel_dir = ""
        elif dirpath.startswith(root_norm + os.sep):
            rel_dir = dirpath[len(root_norm) + 1 :].replace("\\", "/")
        else:
            rel_dir = os.path.relpath(dirpath, long_base).replace("\\", "/").strip(".")

        for fname in filenames:
            lower_name = fname.lower()
            if lower_name == MANIFEST_FILENAME or lower_name.endswith(".part"):
                continue

            full_path = os.path.join(dirpath, fname)
            try:
                st = os.stat(full_path)
            except OSError:
                continue

            local_rel = f"{rel_dir}/{fname}" if rel_dir else fname
            mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
            local_files[_match_key(local_rel)] = {
                "relative_path": local_rel,
                "name": fname,
                "folder": rel_dir or ROOT_FOLDER_LABEL,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "mtime_iso": mtime_iso,
            }

    return local_files


def compare_sharepoint_with_local(
    manifest: dict[str, Any],
    local_dir: str | Path,
    compare_mtime: bool = False,
) -> dict[str, Any]:
    """
    Compare a scanned SharePoint manifest (`folders`, `files`) against `local_dir` on disk,
    returning categorized diffs, folder-by-folder breakdowns, and sync manifests.
    """
    base_dir = Path(local_dir).resolve()
    local_files_map = _scan_local_files(base_dir)
    matched_local_keys: set[str] = set()

    raw_folders = manifest.get("folders") or []
    raw_files = manifest.get("files") or []

    seen_folders: set[str] = set()
    new_folders: list[str] = []
    for raw_folder in raw_folders:
        rel_folder = _normalize_rel_path(str(raw_folder))
        if not rel_folder or rel_folder in seen_folders:
            continue
        seen_folders.add(rel_folder)
        local_folder = build_local_path(base_dir, rel_folder)
        if not os.path.isdir(win_long_path(local_folder)):
            new_folders.append(rel_folder)
    new_folders.sort(key=str.lower)
    new_folder_set = set(new_folders)

    new_files: list[dict[str, Any]] = []
    updated_files: list[dict[str, Any]] = []
    up_to_date_files: list[dict[str, Any]] = []

    folder_buckets: dict[str, dict[str, Any]] = {}

    def _ensure_bucket(folder_label: str) -> dict[str, Any]:
        bucket = folder_buckets.get(folder_label)
        if bucket is None:
            bucket = {
                "folder": folder_label,
                "is_new_folder": folder_label in new_folder_set,
                "new_count": 0,
                "updated_count": 0,
                "up_to_date_count": 0,
                "total_sp_count": 0,
                "new_bytes": 0,
                "updated_bytes": 0,
                "new_file_names": [],
                "updated_file_names": [],
                "new_files": [],
                "updated_files": [],
            }
            folder_buckets[folder_label] = bucket
        return bucket

    for nf in new_folders:
        _ensure_bucket(nf)

    for entry in raw_files:
        if not isinstance(entry, dict):
            continue
        rel_path = _normalize_rel_path(entry.get("relative_path") or entry.get("name") or "")
        if not rel_path:
            continue
        name = entry.get("name") or rel_path.rsplit("/", 1)[-1]
        folder_label = _parent_folder_label(rel_path)
        sp_size = _parse_int_size(entry.get("size"))
        sp_modified = str(entry.get("last_modified") or "")

        sanitized_rel = _sanitized_rel_path(rel_path)
        key = _match_key(sanitized_rel)
        local_info = local_files_map.get(key)

        bucket = _ensure_bucket(folder_label)
        bucket["total_sp_count"] += 1

        if local_info is None:
            item = dict(entry)
            item.update(
                {
                    "relative_path": rel_path,
                    "name": name,
                    "folder": folder_label,
                    "sync_reason": "new",
                    "sp_size": sp_size,
                    "local_size": 0,
                    "sp_modified": sp_modified,
                    "local_mtime": None,
                }
            )
            new_files.append(item)
            bucket["new_count"] += 1
            if sp_size > 0:
                bucket["new_bytes"] += sp_size
            bucket["new_file_names"].append(name)
            bucket["new_files"].append(item)
            continue

        matched_local_keys.add(key)
        local_size = int(local_info["size"])
        local_mtime_epoch = float(local_info["mtime"])
        local_mtime_iso = str(local_info["mtime_iso"])

        size_changed = sp_size >= 0 and local_size != sp_size
        mtime_newer = False
        if compare_mtime:
            sp_ts = _parse_sp_timestamp(sp_modified)
            if sp_ts is not None and sp_ts > local_mtime_epoch + 2.0:
                mtime_newer = True

        if size_changed or mtime_newer:
            if size_changed and mtime_newer:
                update_reason = "size_and_mtime"
            elif size_changed:
                update_reason = "size_changed"
            else:
                update_reason = "newer_modified_time"

            item = dict(entry)
            item.update(
                {
                    "relative_path": rel_path,
                    "name": name,
                    "folder": folder_label,
                    "sync_reason": "updated",
                    "update_reason": update_reason,
                    "local_size": local_size,
                    "sp_size": sp_size,
                    "local_mtime": local_mtime_iso,
                    "local_mtime_epoch": local_mtime_epoch,
                    "sp_modified": sp_modified,
                }
            )
            updated_files.append(item)
            bucket["updated_count"] += 1
            if sp_size > 0:
                bucket["updated_bytes"] += sp_size
            bucket["updated_file_names"].append(name)
            bucket["updated_files"].append(item)
        else:
            item = dict(entry)
            item.update(
                {
                    "relative_path": rel_path,
                    "name": name,
                    "folder": folder_label,
                    "sync_reason": "up_to_date",
                    "local_size": local_size,
                    "sp_size": sp_size,
                    "local_mtime": local_mtime_iso,
                    "local_mtime_epoch": local_mtime_epoch,
                    "sp_modified": sp_modified,
                }
            )
            up_to_date_files.append(item)
            bucket["up_to_date_count"] += 1

    local_only_files: list[dict[str, Any]] = []
    for key, info in local_files_map.items():
        if key not in matched_local_keys:
            local_only_files.append(
                {
                    "relative_path": info["relative_path"],
                    "name": info["name"],
                    "folder": info["folder"],
                    "local_size": info["size"],
                    "local_mtime": info["mtime_iso"],
                    "local_mtime_epoch": info["mtime"],
                }
            )
    local_only_files.sort(key=lambda x: x["relative_path"].lower())

    def _folder_sort_key(bucket: dict[str, Any]) -> tuple[int, str]:
        f_name = bucket["folder"]
        return (0 if f_name == ROOT_FOLDER_LABEL else 1, f_name.lower())

    all_folders_summary = sorted(folder_buckets.values(), key=_folder_sort_key)
    folder_summary = [
        b
        for b in all_folders_summary
        if b["new_count"] > 0 or b["updated_count"] > 0 or b["is_new_folder"]
    ]

    new_only_manifest = {
        "folders": list(new_folders),
        "files": [dict(f) for f in new_files],
    }
    all_changes_files = sorted(
        [dict(f) for f in (*new_files, *updated_files)],
        key=lambda x: x["relative_path"].lower(),
    )
    all_changes_manifest = {
        "folders": list(new_folders),
        "files": all_changes_files,
    }
    sync_manifest = {
        "folders": list(new_folders),
        "files": [dict(f) for f in new_files],
    }

    new_bytes = sum(max(0, f["sp_size"]) for f in new_files)
    updated_bytes = sum(max(0, f["sp_size"]) for f in updated_files)

    summary = {
        "total_sp_folders": len(seen_folders),
        "total_sp_files": len(new_files) + len(updated_files) + len(up_to_date_files),
        "new_folders_count": len(new_folders),
        "new_files_count": len(new_files),
        "new_bytes": new_bytes,
        "updated_files_count": len(updated_files),
        "updated_bytes": updated_bytes,
        "up_to_date_files_count": len(up_to_date_files),
        "local_only_files_count": len(local_only_files),
    }

    return {
        "local_dir": str(base_dir),
        "compared_at": datetime.now(timezone.utc).isoformat(),
        "compare_mtime": bool(compare_mtime),
        "new_folders": new_folders,
        "new_files": new_files,
        "updated_files": updated_files,
        "up_to_date_files": up_to_date_files,
        "local_only_files": local_only_files,
        "folder_summary": folder_summary,
        "all_folders_summary": all_folders_summary,
        "sync_manifest": sync_manifest,
        "new_only_manifest": new_only_manifest,
        "all_changes_manifest": all_changes_manifest,
        "summary": summary,
    }


def build_sync_completion_report(
    sync_plan: dict[str, Any],
    download_snapshot: dict[str, Any] | None,
    local_dir: str | Path,
) -> dict[str, Any]:
    """
    Build post-sync completion summary showing newly downloaded files grouped by folder,
    updated files downloaded, skipped/already-up-to-date counts, and any failed files.
    """
    base_dir = Path(local_dir).resolve()
    plan_new = sync_plan.get("new_files") or []
    plan_updated = sync_plan.get("updated_files") or []
    plan_up_to_date = sync_plan.get("up_to_date_files") or []
    plan_local_only = sync_plan.get("local_only_files") or []
    plan_new_folders = list(sync_plan.get("new_folders") or [])

    new_rel_set = {_normalize_rel_path(f.get("relative_path") or f.get("name") or "") for f in plan_new}
    updated_rel_set = {_normalize_rel_path(f.get("relative_path") or f.get("name") or "") for f in plan_updated}

    newly_downloaded: list[dict[str, Any]] = []
    updated_downloaded: list[dict[str, Any]] = []
    skipped_during_download: list[dict[str, Any]] = []
    failed_files: list[dict[str, Any]] = []
    pending_files: list[dict[str, Any]] = []

    snapshot_files = (download_snapshot or {}).get("files") or []
    for df in snapshot_files:
        if not isinstance(df, dict):
            continue
        rel_path = _normalize_rel_path(df.get("relative_path") or df.get("name") or "")
        name = df.get("name") or (rel_path.rsplit("/", 1)[-1] if rel_path else "")
        folder = _parent_folder_label(rel_path)
        size = _parse_int_size(df.get("size"))
        downloaded_bytes = int(df.get("downloaded_bytes") or 0)
        status = str(df.get("status") or "")
        sync_reason = df.get("sync_reason") or (
            "updated" if rel_path in updated_rel_set else ("new" if rel_path in new_rel_set else "new")
        )

        record = {
            "relative_path": rel_path,
            "name": name,
            "folder": folder,
            "size": size,
            "downloaded_bytes": downloaded_bytes,
            "status": status,
            "sync_reason": sync_reason,
            "attempts": int(df.get("attempts") or 0),
            "error": df.get("error") or "",
        }

        if status == "completed":
            if sync_reason == "updated":
                updated_downloaded.append(record)
            else:
                newly_downloaded.append(record)
        elif status == "skipped":
            skipped_during_download.append(record)
        elif status == "failed":
            failed_files.append(record)
        else:
            pending_files.append(record)

    folder_groups: dict[str, dict[str, Any]] = {}
    for rec in (*newly_downloaded, *updated_downloaded):
        folder = rec["folder"]
        grp = folder_groups.get(folder)
        if grp is None:
            grp = {
                "folder": folder,
                "new_count": 0,
                "updated_count": 0,
                "total_downloaded_count": 0,
                "total_bytes": 0,
                "file_names": [],
                "new_files": [],
                "updated_files": [],
            }
            folder_groups[folder] = grp

        if rec["sync_reason"] == "updated":
            grp["updated_count"] += 1
            grp["updated_files"].append(rec)
        else:
            grp["new_count"] += 1
            grp["new_files"].append(rec)
        grp["total_downloaded_count"] += 1
        byte_val = rec["downloaded_bytes"] if rec["downloaded_bytes"] > 0 else max(0, rec["size"])
        grp["total_bytes"] += byte_val
        grp["file_names"].append(rec["name"])

    downloaded_by_folder = sorted(
        folder_groups.values(),
        key=lambda g: (0 if g["folder"] == ROOT_FOLDER_LABEL else 1, g["folder"].lower()),
    )

    total_downloaded_bytes = sum(
        (r["downloaded_bytes"] if r["downloaded_bytes"] > 0 else max(0, r["size"]))
        for r in (*newly_downloaded, *updated_downloaded)
    )

    already_up_to_date_count = len(plan_up_to_date)
    skipped_download_count = len(skipped_during_download)

    return {
        "local_dir": str(base_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "job_id": (download_snapshot or {}).get("job_id"),
        "job_status": (download_snapshot or {}).get("status", "none"),
        "new_folders": plan_new_folders,
        "newly_downloaded_files": newly_downloaded,
        "newly_downloaded_count": len(newly_downloaded),
        "updated_downloaded_files": updated_downloaded,
        "updated_downloaded_count": len(updated_downloaded),
        "downloaded_by_folder": downloaded_by_folder,
        "already_up_to_date_count": already_up_to_date_count,
        "skipped_download_count": skipped_download_count,
        "skipped_total_count": already_up_to_date_count + skipped_download_count,
        "local_only_count": len(plan_local_only),
        "failed_files": failed_files,
        "failed_count": len(failed_files),
        "pending_files": pending_files,
        "pending_count": len(pending_files),
        "total_downloaded_bytes": total_downloaded_bytes,
    }


def export_sync_csv(report: dict[str, Any]) -> str:
    """Export either a sync completion report or a sync comparison plan as UTF-8 CSV with BOM."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "category",
            "folder",
            "name",
            "relative_path",
            "sp_size",
            "local_size",
            "status",
            "error",
        ]
    )

    is_completion = "newly_downloaded_files" in report or "downloaded_by_folder" in report
    if is_completion:
        groups = [
            ("new_downloaded", report.get("newly_downloaded_files") or [], "completed"),
            ("updated_downloaded", report.get("updated_downloaded_files") or [], "completed"),
            ("failed", report.get("failed_files") or [], "failed"),
            ("pending", report.get("pending_files") or [], "pending"),
        ]
        for category, rows, default_status in groups:
            for item in rows:
                writer.writerow(
                    [
                        category,
                        item.get("folder") or ROOT_FOLDER_LABEL,
                        item.get("name", ""),
                        item.get("relative_path", ""),
                        item.get("size", ""),
                        item.get("downloaded_bytes", ""),
                        item.get("status") or default_status,
                        item.get("error", ""),
                    ]
                )
    else:
        plan_groups = [
            ("new", report.get("new_files") or [], "pending_sync"),
            ("updated", report.get("updated_files") or [], "pending_sync"),
            ("up_to_date", report.get("up_to_date_files") or [], "up_to_date"),
            ("local_only", report.get("local_only_files") or [], "local_only"),
        ]
        for category, rows, status in plan_groups:
            for item in rows:
                writer.writerow(
                    [
                        category,
                        item.get("folder") or ROOT_FOLDER_LABEL,
                        item.get("name", ""),
                        item.get("relative_path", ""),
                        item.get("sp_size", item.get("size", "")),
                        item.get("local_size", ""),
                        status,
                        "",
                    ]
                )

    return "\ufeff" + buf.getvalue()


def export_sync_json(report: dict[str, Any]) -> str:
    """Export a sync plan or sync completion report as UTF-8 formatted JSON."""
    return json.dumps(report, ensure_ascii=False, indent=2)


def calculate_file_hash(file_path: str | Path, algorithm: str = "sha256", chunk_size: int = 1024 * 1024) -> Optional[str]:
    """
    Calculate cryptographic hash (SHA256) of a file using streaming chunks.
    Constant low memory usage (< 2MB) regardless of file size (handles 100GB+ files seamlessly).
    """
    long_path = win_long_path(file_path)
    if not os.path.isfile(long_path):
        return None

    h = hashlib.new(algorithm)
    try:
        with open(long_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def compare_two_local_directories(
    source_dir: str | Path,
    dest_dir: str | Path,
    compare_hash: bool = False,
) -> dict[str, Any]:
    """
    Compare two local or network directories (e.g., Disk-to-Disk, External HDD, NAS share),
    detecting exact matching, modified, missing, and extra files.
    """
    src_path = Path(source_dir).resolve()
    dst_path = Path(dest_dir).resolve()

    src_files = _scan_local_files(src_path)
    dst_files = _scan_local_files(dst_path)

    matched: list[dict[str, Any]] = []
    missing_in_dest: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    extra_in_dest: list[dict[str, Any]] = []

    src_keys = set(src_files.keys())
    dst_keys = set(dst_files.keys())

    total_src_bytes = sum(f["size"] for f in src_files.values())
    total_dst_bytes = sum(f["size"] for f in dst_files.values())

    for key, s_item in src_files.items():
        if key not in dst_files:
            missing_in_dest.append(s_item)
            continue

        d_item = dst_files[key]
        size_match = (s_item["size"] == d_item["size"])

        hash_match = True
        s_hash = None
        d_hash = None
        if compare_hash and size_match:
            s_full = win_long_path(src_path / s_item["relative_path"])
            d_full = win_long_path(dst_path / d_item["relative_path"])
            s_hash = calculate_file_hash(s_full)
            d_hash = calculate_file_hash(d_full)
            hash_match = (s_hash is not None and s_hash == d_hash)

        if size_match and hash_match:
            matched.append({
                **s_item,
                "dest_size": d_item["size"],
                "hash": s_hash,
            })
        else:
            modified.append({
                **s_item,
                "dest_size": d_item["size"],
                "source_hash": s_hash,
                "dest_hash": d_hash,
                "diff_reason": "hash_mismatch" if size_match else "size_mismatch",
            })

    for key, d_item in dst_files.items():
        if key not in src_keys:
            extra_in_dest.append(d_item)

    is_identical = (len(missing_in_dest) == 0 and len(modified) == 0 and len(extra_in_dest) == 0)

    return {
        "source_dir": str(src_path),
        "dest_dir": str(dst_path),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "is_identical": is_identical,
        "total_source_files": len(src_files),
        "total_dest_files": len(dst_files),
        "total_source_bytes": total_src_bytes,
        "total_dest_bytes": total_dst_bytes,
        "matched_count": len(matched),
        "missing_in_dest_count": len(missing_in_dest),
        "modified_count": len(modified),
        "extra_in_dest_count": len(extra_in_dest),
        "matched_files": matched,
        "missing_in_dest_files": missing_in_dest,
        "modified_files": modified,
        "extra_in_dest_files": extra_in_dest,
    }

