"""Tests for v6 backend features: filters, history, notifier, queue, scheduler."""
import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import filters  # noqa: E402
import history_db  # noqa: E402
import notifier  # noqa: E402
from job_queue import JobQueue  # noqa: E402
from scheduler import Scheduler, next_run  # noqa: E402


class FilterTests(unittest.TestCase):
    def test_ext_size_regex(self):
        f = filters.FileFilter.from_dict({
            "include_ext": ["pdf", ".DOCX"], "min_size": 10, "max_size": 1000, "exclude_regex": r"~\$|/tmp/"})
        self.assertTrue(f.matches("a/b/x.PDF", 100))
        self.assertTrue(f.matches("y.docx", 10))
        self.assertFalse(f.matches("x.txt", 100))
        self.assertFalse(f.matches("x.pdf", 5))
        self.assertFalse(f.matches("x.pdf", 5000))
        self.assertFalse(f.matches("a/tmp/x.pdf", 100))

    def test_exclude_ext_and_dates(self):
        f = filters.FileFilter.from_dict({"exclude_ext": "tmp, .bak", "modified_after": "2024-01-01T00:00:00Z",
                                          "modified_before": "2024-12-31"})
        self.assertTrue(f.matches("a.txt", 1, "2024-06-01T10:00:00Z"))
        self.assertFalse(f.matches("a.tmp", 1, "2024-06-01T10:00:00Z"))
        self.assertFalse(f.matches("a.txt", 1, "2023-06-01T10:00:00Z"))
        self.assertFalse(f.matches("a.txt", 1, "2025-06-01T10:00:00Z"))
        self.assertFalse(f.matches("a.txt", 1, None))

    def test_bad_regex_and_inactive(self):
        with self.assertRaises(ValueError):
            filters.FileFilter.from_dict({"exclude_regex": "("})
        self.assertFalse(filters.FileFilter.from_dict(None).active)

    def test_filter_manifest(self):
        m = {"folders": ["a"], "files": [{"relative_path": "a/x.pdf", "size": 5},
                                          {"relative_path": "a/y.txt", "size": 7}]}
        out = filters.filter_manifest(m, {"include_ext": ["pdf"]})
        self.assertEqual(len(out["files"]), 1)
        self.assertEqual(out["total_bytes"], 5)
        self.assertEqual(len(m["files"]), 2)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(history_db, "DB_PATH", Path(self.tmp.name) / "h.db")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_crud_and_csv(self):
        jid = history_db.start_job("download", "src", "dst")
        history_db.finish_job(jid, "completed", 3, 99, "")
        rows = history_db.list_jobs(10)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["files"], 3)
        self.assertIn("completed", history_db.export_csv())
        self.assertEqual(history_db.clear(), 1)
        self.assertEqual(history_db.list_jobs(), [])

    def test_tracker_lifecycle_and_callback(self):
        seen = []
        t = history_db.JobTracker(on_finish=lambda k, s, i: seen.append((k, s, i["files"])))
        t.observe("download", {"status": "idle"})
        t.observe("download", {"status": "running", "job_id": "j1", "dest_dir": "D"})
        t.observe("download", {"status": "completed", "job_id": "j1", "completed_files": 4})
        t.observe("download", {"status": "completed", "job_id": "j1", "completed_files": 4})
        self.assertEqual(seen, [("download", "completed", 4)])
        self.assertEqual(len(history_db.list_jobs()), 1)
        # short job that we never saw running
        t.observe("download", {"status": "failed", "job_id": "j2", "failed_files": 2})
        self.assertEqual(len(history_db.list_jobs()), 2)
        self.assertEqual(history_db.list_jobs()[0]["status"], "failed")


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(notifier, "SETTINGS_PATH", Path(self.tmp.name) / "s.json")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_token_never_returned(self):
        pub = notifier.update_notify_config({"telegram_token": "SECRET123", "telegram_chat_id": "42",
                                             "telegram_enabled": True})
        self.assertNotIn("SECRET123", str(pub))
        self.assertTrue(pub["has_token"])
        # empty token keeps the stored one
        notifier.update_notify_config({"telegram_token": ""})
        self.assertTrue(notifier.public_config()["has_token"])
        notifier.update_notify_config({"clear_token": True})
        self.assertFalse(notifier.public_config()["has_token"])

    def test_notify_channels(self):
        notifier.update_notify_config({"toast": True, "telegram_token": "T", "telegram_chat_id": "1",
                                       "telegram_enabled": True})
        with mock.patch.object(notifier, "_toast", return_value=True) as t, \
                mock.patch.object(notifier, "_telegram", return_value="") as g:
            res = notifier.notify("a", "b")
        t.assert_called_once()
        g.assert_called_once()
        self.assertEqual(res, {"toast": True, "telegram": True})


class QueueTests(unittest.TestCase):
    def test_priority_order_and_cancel(self):
        async def run():
            order = []

            async def runner(kind, params):
                order.append(params["n"])
                await asyncio.sleep(0.01)

            q = JobQueue(runner)
            q.add("download", {"n": 1}, priority=0)
            q.add("sync", {"n": 2}, priority=5)
            c = q.add("media", {"n": 3}, priority=1)
            self.assertTrue(await q.cancel(c["id"]))
            with self.assertRaises(ValueError):
                q.add("bogus", {})
            q.start()
            for _ in range(100):
                if all(j["status"] != "pending" and j["status"] != "running" for j in q.list()):
                    break
                await asyncio.sleep(0.02)
            await q.shutdown()
            return order, q.list()

        order, jobs = asyncio.run(run())
        self.assertEqual(order, [2, 1])
        self.assertEqual(sorted(j["status"] for j in jobs), ["cancelled", "done", "done"])

    def test_runner_failure(self):
        async def run():
            async def runner(kind, params):
                raise RuntimeError("boom")

            q = JobQueue(runner)
            q.start()
            q.add("download", {})
            for _ in range(50):
                if q.list()[0]["status"] == "failed":
                    break
                await asyncio.sleep(0.02)
            await q.shutdown()
            return q.list()[0]

        job = asyncio.run(run())
        self.assertEqual((job["status"], job["error"]), ("failed", "boom"))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sch.json"
        self.addCleanup(self.tmp.cleanup)

    def test_next_run(self):
        base = datetime(2026, 1, 5, 10, 0)  # Monday
        self.assertEqual(next_run({"every": "daily", "time": "09:00"}, base), datetime(2026, 1, 6, 9, 0))
        self.assertEqual(next_run({"every": "daily", "time": "11:30"}, base), datetime(2026, 1, 5, 11, 30))
        self.assertEqual(next_run({"every": "weekly", "time": "08:00", "weekday": 2}, base),
                         datetime(2026, 1, 7, 8, 0))
        self.assertEqual(next_run({"every": "interval_minutes", "minutes": 30}, base), datetime(2026, 1, 5, 10, 30))

    def test_add_tick_persist_remove(self):
        fired = []
        s = Scheduler(lambda k, p: fired.append((k, p)), path=self.path)
        e = s.add({"kind": "download", "every": "interval_minutes", "minutes": 5, "params": {"dest_dir": "x"}})
        self.assertEqual(s.tick(datetime.fromtimestamp(e["created"] + 60)), [])
        self.assertEqual(s.tick(datetime.fromtimestamp(e["created"] + 301)), [e["id"]])
        self.assertEqual(fired, [("download", {"dest_dir": "x"})])
        self.assertEqual(s.tick(datetime.fromtimestamp(e["created"] + 302)), [])  # not re-fired immediately
        s2 = Scheduler(lambda k, p: None, path=self.path)
        self.assertEqual(len(s2.list()), 1)
        self.assertTrue(s2.remove(e["id"]))
        self.assertFalse(s2.remove(e["id"]))

    def test_validation(self):
        s = Scheduler(lambda k, p: None, path=self.path)
        for bad in ({"kind": "x", "every": "daily", "time": "01:00"},
                    {"kind": "sync", "every": "hourly"},
                    {"kind": "sync", "every": "daily", "time": "25:00"}):
            with self.assertRaises(ValueError):
                s.add(bad)


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_mod
        from starlette.testclient import TestClient
        cls.app_mod = app_mod
        cls.client = TestClient(app_mod.app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        for target, attr, val in ((history_db, "DB_PATH", tmp / "h.db"),
                                  (notifier, "SETTINGS_PATH", tmp / "s.json")):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_filters_test_route(self):
        r = self.client.post("/api/filters/test", json={
            "filter": {"include_ext": ["pdf"], "min_size": 10},
            "samples": [{"relative_path": "a.pdf", "size": 50}, {"relative_path": "b.txt", "size": 50}, "c.pdf"]})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual((j["total"], j["matched"]), (3, 1))
        bad = self.client.post("/api/filters/test", json={"filter": {"exclude_regex": "("}, "samples": []})
        self.assertEqual(bad.status_code, 400)

    def test_download_start_applies_filter(self):
        m = {"folders": [], "files": [{"relative_path": "a.pdf", "size": 5}, {"relative_path": "b.txt", "size": 5}]}
        captured = {}

        async def fake_start(**kw):
            captured.update(kw)
            return {"job_id": "J"}

        with mock.patch.object(self.app_mod.state.download_manager, "start_job", side_effect=fake_start):
            r = self.client.post("/api/download/start", json={
                "dest_dir": self.tmp.name, "manifest": m, "filters": {"include_ext": ["pdf"]}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([f["relative_path"] for f in captured["manifest"]["files"]], ["a.pdf"])

    def test_speed_limit_passthrough(self):
        m = {"folders": [], "files": [{"relative_path": "a.pdf", "size": 5}]}
        captured = {}

        async def fake_start(**kw):
            captured.update(kw)
            return {"job_id": "J"}

        with mock.patch.object(self.app_mod.state.download_manager, "start_job", side_effect=fake_start):
            self.client.post("/api/download/start", json={
                "dest_dir": self.tmp.name, "manifest": m, "speed_limit_kbps": 512})
            self.assertEqual(captured["speed_limit_kbps"], 512)
            self.client.post("/api/download/start", json={
                "dest_dir": self.tmp.name, "manifest": m, "speed_limit_kbps": -5})
            self.assertEqual(captured["speed_limit_kbps"], 0)

    def test_dryrun_and_duplicates_routes(self):
        a = Path(self.tmp.name) / "a"
        b = Path(self.tmp.name) / "b"
        a.mkdir()
        b.mkdir()
        (a / "x.txt").write_text("hello")
        (a / "y.txt").write_text("hello")
        r = self.client.post("/api/disk/dryrun", json={"source_dir": str(a), "dest_dir": str(b)})
        self.assertEqual(r.status_code, 200)
        d = self.client.post("/api/disk/duplicates", json={"root": str(a), "min_size": 0})
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json()["group_count"], 1)
        self.assertEqual(self.client.post("/api/disk/dryrun", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/disk/duplicates", json={}).status_code, 400)

    def test_history_routes(self):
        jid = history_db.start_job("media", "u", "d")
        history_db.finish_job(jid, "completed", 1, 2)
        self.assertEqual(len(self.client.get("/api/history?limit=5").json()["jobs"]), 1)
        csv_r = self.client.get("/api/history/export")
        self.assertIn("text/csv", csv_r.headers["content-type"])
        self.assertEqual(self.client.delete("/api/history").json()["deleted"], 1)

    def test_notify_routes_hide_token(self):
        r = self.client.post("/api/settings/notify", json={"telegram_token": "TOPSECRET", "telegram_chat_id": "9"})
        self.assertNotIn("TOPSECRET", r.text)
        self.assertNotIn("TOPSECRET", self.client.get("/api/settings/notify").text)
        with mock.patch.object(notifier, "_toast", return_value=True):
            self.assertTrue(self.client.post("/api/notify/test").json()["ok"])

    def test_queue_and_schedule_routes(self):
        q = self.client.post("/api/queue/add", json={"kind": "media", "params": {}, "priority": 2})
        self.assertEqual(q.status_code, 200)
        jid = q.json()["job"]["id"]
        self.assertTrue(any(j["id"] == jid for j in self.client.get("/api/queue").json()["jobs"]))
        self.assertEqual(self.client.post("/api/queue/cancel", json={"id": jid}).status_code, 200)
        self.assertEqual(self.client.post("/api/queue/add", json={"kind": "zzz"}).status_code, 400)

        sched = self.app_mod.scheduler
        with mock.patch.object(sched, "_path", Path(self.tmp.name) / "sch.json"):
            old = sched.entries
            sched.entries = []
            try:
                r = self.client.post("/api/schedule", json={"kind": "sync", "every": "daily", "time": "03:00"})
                self.assertEqual(r.status_code, 200)
                sid = r.json()["schedule"]["id"]
                self.assertEqual(len(self.client.get("/api/schedule").json()["schedules"]), 1)
                self.assertEqual(self.client.delete(f"/api/schedule?id={sid}").status_code, 200)
                self.assertEqual(self.client.delete("/api/schedule?id=nope").status_code, 404)
            finally:
                sched.entries = old


if __name__ == "__main__":
    unittest.main()
