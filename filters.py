"""File filters for download/sync item lists (FEAT1)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


def _norm_ext(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = re.split(r"[,\s;]+", values)
    out = []
    for v in values or []:
        v = str(v).strip().lower().lstrip("*")
        if v:
            out.append(v if v.startswith(".") else "." + v)
    return tuple(out)


def to_epoch(value: Any) -> Optional[float]:
    """ISO string / epoch number -> epoch seconds (None if unknown)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _opt_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    return max(0, int(v))


@dataclass
class FileFilter:
    include_ext: tuple = ()
    exclude_ext: tuple = ()
    min_size: Optional[int] = None
    max_size: Optional[int] = None
    modified_after: Optional[str] = None
    modified_before: Optional[str] = None
    exclude_regex: Optional[str] = None
    _rx: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.include_ext = _norm_ext(self.include_ext)
        self.exclude_ext = _norm_ext(self.exclude_ext)
        self._rx = re.compile(self.exclude_regex, re.IGNORECASE) if self.exclude_regex else None

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "FileFilter":
        raw = raw if isinstance(raw, dict) else {}
        try:
            return cls(
                include_ext=raw.get("include_ext") or (),
                exclude_ext=raw.get("exclude_ext") or (),
                min_size=_opt_int(raw.get("min_size")),
                max_size=_opt_int(raw.get("max_size")),
                modified_after=raw.get("modified_after") or None,
                modified_before=raw.get("modified_before") or None,
                exclude_regex=str(raw.get("exclude_regex") or "") or None,
            )
        except (re.error, TypeError, ValueError) as exc:
            raise ValueError(f"فلتر غير صالح: {exc}") from exc

    @property
    def active(self) -> bool:
        return any([self.include_ext, self.exclude_ext, self.min_size is not None,
                    self.max_size is not None, self.modified_after, self.modified_before, self._rx])

    def matches(self, rel_path: str, size: int = 0, mtime: Any = None) -> bool:
        path = (rel_path or "").replace("\\", "/")
        name = path.rsplit("/", 1)[-1].lower()
        if self.include_ext and not name.endswith(self.include_ext):
            return False
        if self.exclude_ext and name.endswith(self.exclude_ext):
            return False
        size = int(size or 0)
        if self.min_size is not None and size < self.min_size:
            return False
        if self.max_size is not None and size > self.max_size:
            return False
        if self.modified_after or self.modified_before:
            ts = to_epoch(mtime)
            if ts is None:
                return False
            lo = to_epoch(self.modified_after)
            hi = to_epoch(self.modified_before)
            if lo is not None and ts < lo:
                return False
            if hi is not None and ts > hi:
                return False
        if self._rx and self._rx.search(path):
            return False
        return True

    def match_item(self, item: dict) -> bool:
        rel = item.get("relative_path") or item.get("name") or ""
        mtime = item.get("last_modified") or item.get("mtime") or item.get("modified")
        return self.matches(rel, item.get("size") or 0, mtime)


def filter_manifest(manifest: dict, raw_filter: Optional[dict]) -> dict:
    """Return a copy of manifest with files filtered (folders untouched)."""
    flt = FileFilter.from_dict(raw_filter)
    if not flt.active:
        return manifest
    files = [f for f in (manifest.get("files") or []) if flt.match_item(f)]
    out = dict(manifest)
    out["files"] = files
    out["total_files"] = len(files)
    out["total_bytes"] = sum(int(f.get("size") or 0) for f in files)
    return out


def filter_items(items: Iterable[dict], raw_filter: Optional[dict]) -> list:
    flt = FileFilter.from_dict(raw_filter)
    return [i for i in items if flt.match_item(i)]
