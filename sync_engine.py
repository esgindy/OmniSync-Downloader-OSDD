"""SharePoint <-> Local directory comparison and sync reporting engine."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
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


MTIME_TOLERANCE_SEC = 2.0
DUP_MIN_SIZE = 1024 * 1024
PARTIAL_HASH_BYTES = 64 * 1024


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
    deep: bool = False,
) -> dict[str, Any]:
    """
    Compare two local or network directories (e.g., Disk-to-Disk, External HDD, NAS share),
    detecting exact matching, modified, missing, and extra files.

    Equality is decided by size + mtime (within MTIME_TOLERANCE_SEC) first. SHA-256 (streamed,
    ~1MB buffer) runs only when size matches but mtime differs, or for every size-matched
    file when `deep=True` (`compare_hash=True` is kept as a backward-compatible alias).
    """
    deep = bool(deep or compare_hash)
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
        mtime_close = abs(float(s_item["mtime"]) - float(d_item["mtime"])) <= MTIME_TOLERANCE_SEC
        if size_match and (deep or not mtime_close):
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


def plan_sync_dry_run(
    source_dir: str | Path,
    dest_dir: str | Path,
    deep: bool = False,
    delete_extra: bool = False,
) -> dict[str, Any]:
    """
    Dry run: compare only, copy/delete nothing.
    Signature: plan_sync_dry_run(source_dir, dest_dir, deep=False, delete_extra=False) -> dict
    Returns {"dry_run": True, "to_copy": [...], "to_delete": [...], "copy_count",
    "copy_bytes", "delete_count", "delete_bytes", "report"}; to_copy items feed
    DiskSyncManager.start_sync(); to_delete lists extra dest files only if delete_extra.
    """
    report = compare_two_local_directories(source_dir, dest_dir, deep=deep)
    to_copy = []
    for item in report["missing_in_dest_files"]:
        to_copy.append({**item, "reason": "missing"})
    for item in report["modified_files"]:
        to_copy.append({**item, "reason": item.get("diff_reason", "modified")})
    to_delete = list(report["extra_in_dest_files"]) if delete_extra else []
    return {
        "dry_run": True,
        "source_dir": report["source_dir"],
        "dest_dir": report["dest_dir"],
        "to_copy": to_copy,
        "to_delete": to_delete,
        "copy_count": len(to_copy),
        "copy_bytes": sum(int(f.get("size", 0)) for f in to_copy),
        "delete_count": len(to_delete),
        "delete_bytes": sum(int(f.get("size", 0)) for f in to_delete),
        "report": report,
    }


def _hash_head(path: str, nbytes: int = PARTIAL_HASH_BYTES) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(nbytes)).hexdigest()
    except OSError:
        return None


def find_duplicates(root: str | Path, min_size: int = DUP_MIN_SIZE) -> dict[str, Any]:
    """
    Find identical files under `root`: group by size, then first-64KB hash, then full SHA-256.
    Signature: find_duplicates(root, min_size=1048576) -> dict
    Returns {"root", "groups": [{"hash", "size", "count", "wasted_bytes", "files": [rel paths]}],
    "group_count", "duplicate_files", "wasted_bytes"}; wasted = size * (count - 1).
    """
    root_path = Path(root)
    long_base = win_long_path(root_path)
    by_size: dict[int, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(long_base):
        dirnames.sort()
        for fname in sorted(filenames):
            full = os.path.join(dirpath, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size >= max(1, min_size) and os.path.isfile(full):
                by_size.setdefault(st.st_size, []).append(full)

    base_len = len(long_base.rstrip("/\\")) + 1
    groups: list[dict[str, Any]] = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_part: dict[str, list[str]] = {}
        for p in paths:
            h = _hash_head(p)
            if h:
                by_part.setdefault(h, []).append(p)
        for cands in by_part.values():
            if len(cands) < 2:
                continue
            by_full: dict[str, list[str]] = {}
            for p in cands:
                h = calculate_file_hash(p)
                if h:
                    by_full.setdefault(h, []).append(p)
            for h, same in by_full.items():
                if len(same) > 1:
                    groups.append({
                        "hash": h,
                        "size": size,
                        "count": len(same),
                        "wasted_bytes": size * (len(same) - 1),
                        "files": sorted(p[base_len:].replace("\\", "/") for p in same),
                    })
    groups.sort(key=lambda g: g["wasted_bytes"], reverse=True)
    return {
        "root": str(root_path),
        "groups": groups,
        "group_count": len(groups),
        "duplicate_files": sum(g["count"] - 1 for g in groups),
        "wasted_bytes": sum(g["wasted_bytes"] for g in groups),
    }


def find_duplicates_multi(roots: list[str | Path], min_size: int = DUP_MIN_SIZE) -> dict[str, Any]:
    """
    Find identical files inside one or two roots (A, B), including copies shared between them.
    Group by size, then first-64KB hash, then full SHA-256.
    Returns {"roots", "groups": [{"hash", "size", "count", "wasted_bytes", "files": [{"root_index",
    "rel", "path", "size", "mtime"}]}], "group_count", "duplicate_files", "wasted_bytes",
    "cross_groups"}; cross_groups counts groups that exist in more than one root.
    """
    clean: list[Path] = []
    for r in roots:
        s = str(r or "").strip()
        if s:
            clean.append(Path(s))
    if not clean:
        raise ValueError("no root given")
    for r in clean:
        if not os.path.isdir(win_long_path(r)):
            raise ValueError(f"not a folder: {r}")

    by_size: dict[int, list[tuple[int, str, str]]] = {}
    for idx, root_path in enumerate(clean):
        long_base = win_long_path(root_path)
        base_len = len(long_base.rstrip("/\\")) + 1
        for dirpath, dirnames, filenames in os.walk(long_base):
            dirnames.sort()
            for fname in sorted(filenames):
                full = os.path.join(dirpath, fname)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size >= max(1, min_size) and os.path.isfile(full) and not os.path.islink(full):
                    by_size.setdefault(st.st_size, []).append((idx, full, full[base_len:]))

    groups: list[dict[str, Any]] = []
    for size, entries in by_size.items():
        if len(entries) < 2:
            continue
        by_part: dict[str, list[tuple[int, str, str]]] = {}
        for ent in entries:
            h = _hash_head(ent[1])
            if h:
                by_part.setdefault(h, []).append(ent)
        for cands in by_part.values():
            if len(cands) < 2:
                continue
            by_full: dict[str, list[tuple[int, str, str]]] = {}
            for ent in cands:
                h = calculate_file_hash(ent[1])
                if h:
                    by_full.setdefault(h, []).append(ent)
            for h, same in by_full.items():
                if len(same) < 2:
                    continue
                files = []
                for idx, full, rel in same:
                    try:
                        mtime = os.stat(full).st_mtime
                    except OSError:
                        mtime = 0.0
                    files.append({
                        "root_index": idx,
                        "rel": rel.replace("\\", "/"),
                        "path": os.path.join(str(clean[idx]), rel),
                        "size": size,
                        "mtime": mtime,
                    })
                files.sort(key=lambda f: (f["root_index"], f["rel"]))
                groups.append({
                    "hash": h,
                    "size": size,
                    "count": len(files),
                    "wasted_bytes": size * (len(files) - 1),
                    "files": files,
                })
    groups.sort(key=lambda g: g["wasted_bytes"], reverse=True)
    return {
        "roots": [str(r) for r in clean],
        "groups": groups,
        "group_count": len(groups),
        "duplicate_files": sum(g["count"] - 1 for g in groups),
        "wasted_bytes": sum(g["wasted_bytes"] for g in groups),
        "cross_groups": sum(1 for g in groups if len({f["root_index"] for f in g["files"]}) > 1),
    }


DUP_KEEP_STRATEGIES = ("root:0", "root:1", "oldest", "newest", "shortest_path")


def plan_duplicate_removal(
    groups: list[dict[str, Any]],
    keep: str = "root:0",
    dedupe_within_kept: bool = False,
    excluded_paths: Optional[set[str]] = None,
) -> dict[str, Any]:
    """
    Dry run: decide which copies stay and which go. Nothing is deleted here.
    keep: "root:0" (A stays), "root:1" (B stays), "oldest", "newest" or "shortest_path".
    With root:N all copies inside root N stay (unless dedupe_within_kept) and copies elsewhere go;
    if a group has no copy in root N, one copy is kept (the first) and the rest go.
    Files in excluded_paths are never deleted. At least one copy of every group always stays.
    """
    if keep not in DUP_KEEP_STRATEGIES:
        raise ValueError("invalid keep strategy")
    excluded = {os.path.normcase(os.path.abspath(p)) for p in (excluded_paths or set())}
    items: list[dict[str, Any]] = []
    kept_total = 0
    for g in groups:
        files = list(g.get("files") or [])
        if len(files) < 2:
            continue
        if keep.startswith("root:"):
            want = int(keep.split(":", 1)[1])
            preferred = [f for f in files if f.get("root_index") == want]
            if preferred:
                keepers = preferred[:1] if dedupe_within_kept else preferred
            else:
                keepers = files[:1]
        elif keep == "oldest":
            keepers = [min(files, key=lambda f: (f.get("mtime", 0), f["path"]))]
        elif keep == "newest":
            keepers = [max(files, key=lambda f: (f.get("mtime", 0), f["path"]))]
        else:
            keepers = [min(files, key=lambda f: (len(f["path"]), f["path"]))]
        keeper_paths = {f["path"] for f in keepers}
        main_keeper = keepers[0]["path"]
        kept_total += len(keepers)
        for f in files:
            if f["path"] in keeper_paths:
                continue
            if os.path.normcase(os.path.abspath(f["path"])) in excluded:
                kept_total += 1
                continue
            items.append({
                "path": f["path"],
                "keeper": main_keeper,
                "hash": g.get("hash"),
                "size": int(g.get("size", f.get("size", 0))),
                "root_index": f.get("root_index", 0),
            })
    return {
        "dry_run": True,
        "keep": keep,
        "items": items,
        "delete_count": len(items),
        "delete_bytes": sum(i["size"] for i in items),
        "kept_count": kept_total,
    }


def _under_any_root(path: str, roots: list[str]) -> bool:
    p = os.path.normcase(os.path.realpath(path))
    for r in roots:
        base = os.path.normcase(os.path.realpath(r))
        if p != base and p.startswith(base.rstrip("\\/") + os.sep):
            return True
    return False


def apply_duplicate_removal(items: list[dict[str, Any]], roots: list[str]) -> dict[str, Any]:
    """
    Move duplicate files to the Recycle Bin (never a permanent delete). Every item is re-verified
    right before removal: path under a scanned root, keeper still exists, same size and same SHA-256.
    Requires send2trash; without it nothing is removed.
    """
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise RuntimeError("send2trash غير مثبت: pip install send2trash") from exc

    keepers = {os.path.normcase(os.path.abspath(i.get("keeper", ""))) for i in items}
    removed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in items:
        path = str(item.get("path") or "")
        keeper = str(item.get("keeper") or "")
        expected = item.get("hash")

        def skip(reason: str) -> None:
            skipped.append({"path": path, "reason": reason})

        if not path or not keeper or not expected:
            skip("بيانات ناقصة")
            continue
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(keeper)):
            skip("المسار هو نفسه النسخة المحفوظة")
            continue
        if os.path.normcase(os.path.abspath(path)) in keepers:
            skip("نسخة محفوظة لمجموعة أخرى")
            continue
        if not (_under_any_root(path, roots) and _under_any_root(keeper, roots)):
            skip("خارج المجلدات المفحوصة")
            continue
        lp, lk = win_long_path(Path(path)), win_long_path(Path(keeper))
        if os.path.islink(lp) or not os.path.isfile(lp):
            skip("الملف غير موجود")
            continue
        if not os.path.isfile(lk):
            skip("النسخة المحفوظة غير موجودة")
            continue
        try:
            size = int(item.get("size", -1))
            if os.path.getsize(lp) != size or os.path.getsize(lk) != size:
                skip("اختلف الحجم منذ الفحص")
                continue
        except OSError:
            skip("تعذر قراءة الحجم")
            continue
        if calculate_file_hash(lp) != expected or calculate_file_hash(lk) != expected:
            skip("اختلف المحتوى منذ الفحص")
            continue
        try:
            send2trash(path)
        except Exception as exc:  # noqa: BLE001
            skip(f"فشل النقل إلى السلة: {exc}")
            continue
        removed.append({"path": path, "size": size})
    return {
        "removed": removed,
        "skipped": skipped,
        "removed_count": len(removed),
        "freed_bytes": sum(r["size"] for r in removed),
        "skipped_count": len(skipped),
    }


class DiskSyncManager:
    """High-performance Drive-to-Drive / Disk-to-Disk sync engine with streaming chunked copy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._worker_thread: Optional[threading.Thread] = None

        self._status: str = "idle"  # idle, running, paused, completed, cancelled, error
        self._error: Optional[str] = None
        self._source_dir: str = ""
        self._dest_dir: str = ""
        self._total_files: int = 0
        self._copied_files: int = 0
        self._total_bytes: int = 0
        self._copied_bytes: int = 0
        self._current_file: str = ""
        self._speed_bps: float = 0.0
        self._eta_seconds: Optional[float] = None
        self._last_sample_time: float = 0.0
        self._last_sample_bytes: int = 0
        self._sync_log: list[dict[str, Any]] = []

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            pct = (
                (self._copied_bytes / self._total_bytes * 100.0)
                if self._total_bytes > 0
                else (self._copied_files / self._total_files * 100.0 if self._total_files > 0 else 0.0)
            )
            return {
                "status": self._status,
                "error": self._error,
                "source_dir": self._source_dir,
                "dest_dir": self._dest_dir,
                "total_files": self._total_files,
                "copied_files": self._copied_files,
                "remaining_files": max(0, self._total_files - self._copied_files),
                "total_bytes": self._total_bytes,
                "copied_bytes": self._copied_bytes,
                "remaining_bytes": max(0, self._total_bytes - self._copied_bytes),
                "current_file": self._current_file,
                "speed_bps": round(self._speed_bps, 2),
                "eta_seconds": self._eta_seconds,
                "progress_pct": round(min(100.0, pct), 2),
                "recent_files": self._sync_log[-30:],
            }

    def start_sync(
        self,
        source_dir: str | Path,
        dest_dir: str | Path,
        files_to_sync: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            if self._status == "running":
                raise RuntimeError("توجد عملية مزامنة قيد التشغيل بالفعل")

            self._source_dir = str(source_dir)
            self._dest_dir = str(dest_dir)
            self._total_files = len(files_to_sync)
            self._copied_files = 0
            self._total_bytes = sum(f.get("size", 0) for f in files_to_sync)
            self._copied_bytes = 0
            self._current_file = ""
            self._speed_bps = 0.0
            self._eta_seconds = None
            self._status = "running"
            self._error = None
            self._sync_log = []
            self._stop_event.clear()
            self._pause_event.set()

            self._worker_thread = threading.Thread(
                target=self._run_copy_loop,
                args=(source_dir, dest_dir, files_to_sync),
                daemon=True,
            )
            self._worker_thread.start()

    @staticmethod
    def dry_run(source_dir, dest_dir, deep: bool = False, delete_extra: bool = False) -> dict[str, Any]:
        """Plan only; see plan_sync_dry_run. Copies nothing."""
        return plan_sync_dry_run(source_dir, dest_dir, deep=deep, delete_extra=delete_extra)

    def pause(self) -> None:
        with self._lock:
            if self._status == "running":
                self._pause_event.clear()
                self._status = "paused"

    def resume(self) -> None:
        with self._lock:
            if self._status == "paused":
                self._pause_event.set()
                self._status = "running"

    def cancel(self) -> None:
        with self._lock:
            self._stop_event.set()
            self._pause_event.set()
            if self._status in ("running", "paused"):
                self._status = "cancelled"

    def _run_copy_loop(
        self,
        source_dir: str | Path,
        dest_dir: str | Path,
        items: list[dict[str, Any]],
        chunk_size: int = 1024 * 1024,
    ) -> None:
        src_base = Path(source_dir)
        dst_base = Path(dest_dir)
        self._last_sample_time = time.monotonic()
        self._last_sample_bytes = 0

        for item in items:
            if self._stop_event.is_set():
                with self._lock:
                    self._status = "cancelled"
                return

            while not self._pause_event.is_set():
                time.sleep(0.2)
                if self._stop_event.is_set():
                    with self._lock:
                        self._status = "cancelled"
                    return

            rel_path = item.get("relative_path") or item.get("name") or ""
            with self._lock:
                self._current_file = rel_path

            src_file = win_long_path(build_local_path(src_base, rel_path))
            dst_file = win_long_path(build_local_path(dst_base, rel_path))

            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            dst_part = dst_file + ".sync_part"

            try:
                with open(src_file, "rb") as fsrc, open(dst_part, "wb") as fdst:
                    while True:
                        if self._stop_event.is_set():
                            try:
                                os.remove(dst_part)
                            except OSError:
                                pass
                            with self._lock:
                                self._status = "cancelled"
                            return

                        while not self._pause_event.is_set():
                            time.sleep(0.2)
                            if self._stop_event.is_set():
                                try:
                                    os.remove(dst_part)
                                except OSError:
                                    pass
                                with self._lock:
                                    self._status = "cancelled"
                                return

                        buf = fsrc.read(chunk_size)
                        if not buf:
                            break
                        fdst.write(buf)

                        chunk_len = len(buf)
                        with self._lock:
                            self._copied_bytes += chunk_len

                        now = time.monotonic()
                        dt = now - self._last_sample_time
                        if dt >= 0.35:
                            bytes_diff = self._copied_bytes - self._last_sample_bytes
                            speed = bytes_diff / dt if dt > 0 else 0.0
                            self._speed_bps = speed
                            self._last_sample_time = now
                            self._last_sample_bytes = self._copied_bytes
                            rem = max(0, self._total_bytes - self._copied_bytes)
                            if speed > 10.0:
                                self._eta_seconds = round(rem / speed, 1)

                try:
                    shutil.copystat(src_file, dst_part)
                except OSError:
                    pass

                if os.path.exists(dst_file):
                    os.remove(dst_file)
                os.replace(dst_part, dst_file)

                with self._lock:
                    self._copied_files += 1
                    self._sync_log.append({
                        "relative_path": rel_path,
                        "size": item.get("size", 0),
                        "status": "success",
                        "time": datetime.now(timezone.utc).isoformat(),
                    })

            except Exception as exc:
                try:
                    if os.path.exists(dst_part):
                        os.remove(dst_part)
                except OSError:
                    pass
                with self._lock:
                    self._sync_log.append({
                        "relative_path": rel_path,
                        "size": item.get("size", 0),
                        "status": "error",
                        "error": str(exc),
                        "time": datetime.now(timezone.utc).isoformat(),
                    })

        with self._lock:
            if not self._stop_event.is_set():
                self._status = "completed"
                self._speed_bps = 0.0
                self._eta_seconds = 0.0
                self._current_file = ""


