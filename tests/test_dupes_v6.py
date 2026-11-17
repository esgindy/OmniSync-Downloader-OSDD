"""Tests for the duplicate review tab: multi-root scan, removal plan, safe apply."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync_engine  # noqa: E402


def _w(root: str, rel: str, data: bytes, mtime: float | None = None) -> str:
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class DupesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osdd_dupes_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.a = os.path.join(self.tmp, "A")
        self.b = os.path.join(self.tmp, "B")
        self.trash = os.path.join(self.tmp, "_trash")
        for d in (self.a, self.b, self.trash):
            os.makedirs(d)

    def fake_send2trash(self):
        def _send(path):
            shutil.move(path, os.path.join(self.trash, os.path.basename(path) + str(len(os.listdir(self.trash)))))
        mod = types.ModuleType("send2trash")
        mod.send2trash = _send
        return mock.patch.dict(sys.modules, {"send2trash": mod})


class ScanTests(DupesBase):
    def test_cross_root_and_internal(self):
        _w(self.a, "x.txt", b"same-content")
        _w(self.b, "y.txt", b"same-content")
        _w(self.a, "sub/z.txt", b"same-content")
        _w(self.a, "only.txt", b"unique-A")
        res = sync_engine.find_duplicates_multi([self.a, self.b], min_size=0)
        self.assertEqual(res["group_count"], 1)
        self.assertEqual(res["groups"][0]["count"], 3)
        self.assertEqual(res["cross_groups"], 1)
        self.assertEqual({f["root_index"] for f in res["groups"][0]["files"]}, {0, 1})

    def test_single_root(self):
        _w(self.a, "1.txt", b"dup")
        _w(self.a, "2.txt", b"dup")
        res = sync_engine.find_duplicates_multi([self.a], min_size=0)
        self.assertEqual(res["group_count"], 1)
        self.assertEqual(res["cross_groups"], 0)

    def test_min_size_filters_small(self):
        _w(self.a, "1.txt", b"dup")
        _w(self.b, "2.txt", b"dup")
        res = sync_engine.find_duplicates_multi([self.a, self.b], min_size=1024)
        self.assertEqual(res["group_count"], 0)

    def test_bad_root_raises(self):
        with self.assertRaises(ValueError):
            sync_engine.find_duplicates_multi([os.path.join(self.tmp, "nope")])
        with self.assertRaises(ValueError):
            sync_engine.find_duplicates_multi([])


class PlanTests(DupesBase):
    def _groups(self):
        _w(self.a, "x.txt", b"same", 3000)
        _w(self.a, "x2.txt", b"same", 2000)
        _w(self.b, "y.txt", b"same", 1000)
        return sync_engine.find_duplicates_multi([self.a, self.b], min_size=0)["groups"]

    def test_keep_root_a_deletes_b_only(self):
        plan = sync_engine.plan_duplicate_removal(self._groups(), keep="root:0")
        self.assertEqual(plan["delete_count"], 1)
        self.assertTrue(plan["items"][0]["path"].startswith(self.b))
        self.assertEqual(plan["kept_count"], 2)

    def test_keep_root_a_dedupe_within(self):
        plan = sync_engine.plan_duplicate_removal(self._groups(), keep="root:0", dedupe_within_kept=True)
        self.assertEqual(plan["delete_count"], 2)
        self.assertEqual(plan["kept_count"], 1)

    def test_keep_root_b(self):
        plan = sync_engine.plan_duplicate_removal(self._groups(), keep="root:1")
        self.assertEqual(plan["delete_count"], 2)
        self.assertTrue(all(i["path"].startswith(self.a) for i in plan["items"]))

    def test_oldest_newest_keep_exactly_one(self):
        old = sync_engine.plan_duplicate_removal(self._groups(), keep="oldest")
        new = sync_engine.plan_duplicate_removal(self._groups(), keep="newest")
        self.assertEqual(old["delete_count"], 2)
        self.assertEqual(new["delete_count"], 2)
        self.assertTrue(old["items"][0]["keeper"].endswith("y.txt"))
        self.assertTrue(new["items"][0]["keeper"].endswith("x.txt"))

    def test_keep_root_missing_falls_back_to_one_copy(self):
        _w(self.a, "p.txt", b"only-in-a")
        _w(self.a, "q.txt", b"only-in-a")
        groups = sync_engine.find_duplicates_multi([self.a, self.b], min_size=0)["groups"]
        plan = sync_engine.plan_duplicate_removal(groups, keep="root:1")
        self.assertEqual(plan["delete_count"], 1)

    def test_excluded_never_deleted(self):
        groups = self._groups()
        target = os.path.join(self.b, "y.txt")
        plan = sync_engine.plan_duplicate_removal(groups, keep="root:0", excluded_paths={target})
        self.assertEqual(plan["delete_count"], 0)

    def test_invalid_strategy(self):
        with self.assertRaises(ValueError):
            sync_engine.plan_duplicate_removal([], keep="bogus")

    def test_group_always_keeps_one(self):
        for strat in sync_engine.DUP_KEEP_STRATEGIES:
            plan = sync_engine.plan_duplicate_removal(self._groups(), keep=strat)
            self.assertLessEqual(plan["delete_count"], 2, strat)
            self.assertGreaterEqual(plan["kept_count"], 1, strat)


class ApplyTests(DupesBase):
    def _plan(self):
        _w(self.a, "x.txt", b"same")
        _w(self.b, "y.txt", b"same")
        groups = sync_engine.find_duplicates_multi([self.a, self.b], min_size=0)["groups"]
        return sync_engine.plan_duplicate_removal(groups, keep="root:0")

    def test_apply_moves_to_trash_only_duplicate(self):
        plan = self._plan()
        with self.fake_send2trash():
            res = sync_engine.apply_duplicate_removal(plan["items"], [self.a, self.b])
        self.assertEqual(res["removed_count"], 1)
        self.assertEqual(res["freed_bytes"], 4)
        self.assertTrue(os.path.exists(os.path.join(self.a, "x.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.b, "y.txt")))

    def test_skips_when_content_changed(self):
        plan = self._plan()
        _w(self.b, "y.txt", b"diff")
        with self.fake_send2trash():
            res = sync_engine.apply_duplicate_removal(plan["items"], [self.a, self.b])
        self.assertEqual(res["removed_count"], 0)
        self.assertEqual(res["skipped_count"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.b, "y.txt")))

    def test_skips_when_keeper_missing(self):
        plan = self._plan()
        os.remove(os.path.join(self.a, "x.txt"))
        with self.fake_send2trash():
            res = sync_engine.apply_duplicate_removal(plan["items"], [self.a, self.b])
        self.assertEqual(res["removed_count"], 0)
        self.assertTrue(os.path.exists(os.path.join(self.b, "y.txt")))

    def test_refuses_path_outside_roots(self):
        outside = _w(self.tmp, "outside.txt", b"same")
        keeper = os.path.join(self.a, "x.txt")
        _w(self.a, "x.txt", b"same")
        h = sync_engine.calculate_file_hash(keeper)
        item = {"path": outside, "keeper": keeper, "hash": h, "size": 4}
        with self.fake_send2trash():
            res = sync_engine.apply_duplicate_removal([item], [self.a, self.b])
        self.assertEqual(res["removed_count"], 0)
        self.assertTrue(os.path.exists(outside))

    def test_never_deletes_a_keeper_of_another_item(self):
        pa = _w(self.a, "x.txt", b"same")
        pb = _w(self.b, "y.txt", b"same")
        h = sync_engine.calculate_file_hash(pa)
        items = [
            {"path": pa, "keeper": pb, "hash": h, "size": 4},
            {"path": pb, "keeper": pa, "hash": h, "size": 4},
        ]
        with self.fake_send2trash():
            res = sync_engine.apply_duplicate_removal(items, [self.a, self.b])
        self.assertEqual(res["removed_count"], 0)
        self.assertTrue(os.path.exists(pa) and os.path.exists(pb))

    def test_no_send2trash_means_no_deletion(self):
        plan = self._plan()
        with mock.patch.dict(sys.modules, {"send2trash": None}):
            with self.assertRaises(RuntimeError):
                sync_engine.apply_duplicate_removal(plan["items"], [self.a, self.b])
        self.assertTrue(os.path.exists(os.path.join(self.b, "y.txt")))


class ApiTests(DupesBase):
    def setUp(self):
        super().setUp()
        import app as app_mod
        from starlette.testclient import TestClient
        client = TestClient(app_mod.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.client = client

    def test_scan_plan_apply_flow(self):
        _w(self.a, "x.txt", b"same")
        _w(self.b, "y.txt", b"same")
        r = self.client.post("/api/dupes/scan", json={"root_a": self.a, "root_b": self.b, "min_size": 0})
        self.assertEqual(r.status_code, 200)
        groups = r.json()["groups"]
        r = self.client.post("/api/dupes/plan", json={"groups": groups, "keep": "root:0"})
        self.assertEqual(r.status_code, 200)
        items = r.json()["items"]
        self.assertEqual(len(items), 1)
        body = {"items": items, "root_a": self.a, "root_b": self.b}
        self.assertEqual(self.client.post("/api/dupes/apply", json=body).status_code, 400)  # no confirm
        with self.fake_send2trash():
            r = self.client.post("/api/dupes/apply", json={**body, "confirm": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["removed_count"], 1)

    def test_scan_requires_root(self):
        self.assertEqual(self.client.post("/api/dupes/scan", json={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
