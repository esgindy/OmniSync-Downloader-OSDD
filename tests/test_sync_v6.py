"""Tests for v6 sync: mtime-first compare, dry-run, duplicate finder."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import sync_engine  # noqa: E402


def _w(root, rel, data, mtime=None):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.src = Path(self.td.name) / "s"
        self.dst = Path(self.td.name) / "d"
        self.src.mkdir()
        self.dst.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_same_size_mtime_skips_hash(self):
        _w(self.src, "a.bin", b"xxxx", 1000)
        _w(self.dst, "a.bin", b"yyyy", 1001)  # within 2s -> equal, no hash
        with mock.patch.object(sync_engine, "calculate_file_hash") as m:
            r = sync_engine.compare_two_local_directories(self.src, self.dst)
        m.assert_not_called()
        self.assertEqual(r["matched_count"], 1)

    def test_mtime_differs_hashes(self):
        _w(self.src, "a.bin", b"xxxx", 1000)
        _w(self.dst, "a.bin", b"yyyy", 5000)
        _w(self.src, "b.bin", b"same", 1000)
        _w(self.dst, "b.bin", b"same", 5000)
        r = sync_engine.compare_two_local_directories(self.src, self.dst)
        self.assertEqual(r["modified_count"], 1)
        self.assertEqual(r["modified_files"][0]["diff_reason"], "hash_mismatch")
        self.assertEqual(r["matched_count"], 1)

    def test_deep_forces_hash(self):
        _w(self.src, "a.bin", b"xxxx", 1000)
        _w(self.dst, "a.bin", b"yyyy", 1000)
        r = sync_engine.compare_two_local_directories(self.src, self.dst, deep=True)
        self.assertEqual(r["modified_count"], 1)

    def test_size_mismatch_and_missing_extra(self):
        _w(self.src, "a", b"1")
        _w(self.dst, "a", b"12")
        _w(self.src, "m", b"1")
        _w(self.dst, "e", b"1")
        r = sync_engine.compare_two_local_directories(self.src, self.dst)
        self.assertEqual(r["modified_files"][0]["diff_reason"], "size_mismatch")
        self.assertEqual(r["missing_in_dest_count"], 1)
        self.assertEqual(r["extra_in_dest_count"], 1)


class DryRunTests(unittest.TestCase):
    def test_dry_run_copies_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / "s", Path(td) / "d"
            _w(src, "new.txt", b"hello")
            _w(src, "chg.txt", b"abc")
            _w(dst, "chg.txt", b"abcdef")
            _w(dst, "old.txt", b"zz")
            plan = sync_engine.DiskSyncManager.dry_run(src, dst, delete_extra=True)
            self.assertTrue(plan["dry_run"])
            self.assertEqual(plan["copy_count"], 2)
            self.assertEqual(plan["copy_bytes"], 8)
            self.assertEqual(plan["delete_count"], 1)
            self.assertEqual(plan["delete_bytes"], 2)
            self.assertFalse((dst / "new.txt").exists())
            self.assertTrue((dst / "old.txt").exists())
            plan2 = sync_engine.plan_sync_dry_run(src, dst)
            self.assertEqual(plan2["delete_count"], 0)


class DuplicateTests(unittest.TestCase):
    def test_groups_and_wasted(self):
        with tempfile.TemporaryDirectory() as td:
            a = b"A" * 2000 + b"1"
            _w(td, "x/a1.bin", a)
            _w(td, "y/a2.bin", a)
            _w(td, "a3.bin", a)
            _w(td, "diff_tail.bin", b"A" * 2000 + b"2")  # same size/head, other tail
            _w(td, "small.bin", b"s")
            r = sync_engine.find_duplicates(td, min_size=1000)
            self.assertEqual(r["group_count"], 1)
            g = r["groups"][0]
            self.assertEqual(g["count"], 3)
            self.assertEqual(g["wasted_bytes"], 2001 * 2)
            self.assertEqual(sorted(g["files"]), ["a3.bin", "x/a1.bin", "y/a2.bin"])
            self.assertEqual(r["wasted_bytes"], 4002)

    def test_min_size_filters(self):
        with tempfile.TemporaryDirectory() as td:
            _w(td, "a", b"q" * 10)
            _w(td, "b", b"q" * 10)
            self.assertEqual(sync_engine.find_duplicates(td)["group_count"], 0)


if __name__ == "__main__":
    unittest.main()
