"""Offline tests for v6 perf features: parallel Range, token bucket, backoff, http2 fallback."""

from __future__ import annotations

import asyncio
import http.server
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import downloader  # noqa: E402
from downloader import DownloadManager, _TokenBucket, _split_ranges  # noqa: E402
from sp_auth import AuthSession, BrowserAuthManager  # noqa: E402
from sp_client import SharePointClient, http2_available, jitter_backoff, parse_retry_after  # noqa: E402

PAYLOAD = bytes((i * 31 + 7) % 256 for i in range(600_000))


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    ranges_ok = True
    seen_ranges: list[str] = []

    def log_message(self, *a):  # noqa: D401
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng:
            type(self).seen_ranges.append(rng)
        if rng and type(self).ranges_ok and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            a = int(a)
            b = int(b) if b else len(PAYLOAD) - 1
            body = PAYLOAD[a : b + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(PAYLOAD)}")
        else:
            body = PAYLOAD
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes" if type(self).ranges_ok else "none")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Srv:
    def __enter__(self):
        _Handler.seen_ranges = []
        _Handler.ranges_ok = True
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def _manager(base: str) -> DownloadManager:
    mgr = BrowserAuthManager()
    mgr.session = AuthSession(origin=base, web_url=base, cookies=[], authenticated=True)
    return DownloadManager(SharePointClient(mgr))


async def _run(dm: DownloadManager, tmp: str, **kw) -> dict:
    manifest = {"files": [{"name": "big.bin", "relative_path": "big.bin", "size": len(PAYLOAD),
                           "download_url": kw.pop("url")}]}
    await dm.start_job(dest_dir=tmp, manifest=manifest, concurrency=1, max_retries=2, **kw)
    deadline = time.monotonic() + 20
    while dm.get_snapshot()["status"] not in ("completed", "cancelled"):
        if time.monotonic() > deadline:
            raise AssertionError("timeout")
        await asyncio.sleep(0.05)
    return dm.get_snapshot()


class PerfHelpersTest(unittest.TestCase):
    def test_split_ranges_cover_exactly(self):
        r = _split_ranges(1003, 4)
        self.assertEqual(r[0][0], 0)
        self.assertEqual(r[-1][1], 1002)
        for (a, b), (c, d) in zip(r, r[1:]):
            self.assertEqual(b + 1, c)

    def test_retry_after_and_backoff(self):
        self.assertEqual(parse_retry_after("7"), 7.0)
        self.assertIsNone(parse_retry_after("garbage"))
        self.assertGreaterEqual(parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT"), 1000)
        for n in range(8):
            d = jitter_backoff(n, base=1.0, cap=10.0)
            self.assertLessEqual(d, 10.0)
            self.assertGreaterEqual(d, min(10.0, 2 ** n) / 2)

    def test_http2_flag_is_bool(self):
        self.assertIsInstance(http2_available(), bool)


class TokenBucketTest(unittest.IsolatedAsyncioTestCase):
    async def test_unlimited_is_instant(self):
        b = _TokenBucket(0)
        t = time.monotonic()
        await b.consume(10_000_000)
        self.assertLess(time.monotonic() - t, 0.2)

    async def test_limit_slows_down(self):
        b = _TokenBucket(100_000)  # 100KB/s, burst 100KB
        t = time.monotonic()
        for _ in range(4):
            await b.consume(50_000)  # 200KB total -> ~1s after burst
        self.assertGreaterEqual(time.monotonic() - t, 0.7)


class ParallelDownloadTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old = (downloader.PARALLEL_THRESHOLD_BYTES, downloader.PARALLEL_MIN_PART_BYTES)
        downloader.PARALLEL_THRESHOLD_BYTES = 100_000
        downloader.PARALLEL_MIN_PART_BYTES = 50_000

    def tearDown(self):
        downloader.PARALLEL_THRESHOLD_BYTES, downloader.PARALLEL_MIN_PART_BYTES = self._old

    async def test_parallel_range_download(self):
        with _Srv() as srv, tempfile.TemporaryDirectory() as tmp:
            dm = _manager(srv.base)
            snap = await _run(dm, tmp, url=srv.base + "/f", speed_limit_kbps=0)
            self.assertEqual(snap["files"][0]["status"], "completed")
            self.assertEqual(snap["speed_limit_kbps"], 0)
            with open(os.path.join(tmp, "big.bin"), "rb") as fh:
                self.assertEqual(fh.read(), PAYLOAD)
            self.assertGreaterEqual(len(_Handler.seen_ranges), 5)  # probe + >=4 parts
            self.assertFalse(os.path.exists(os.path.join(tmp, "big.bin.part")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "big.bin.part.pdl")))

    async def test_fallback_when_ranges_unsupported(self):
        with _Srv() as srv, tempfile.TemporaryDirectory() as tmp:
            _Handler.ranges_ok = False
            dm = _manager(srv.base)
            snap = await _run(dm, tmp, url=srv.base + "/f")
            self.assertEqual(snap["files"][0]["status"], "completed")
            with open(os.path.join(tmp, "big.bin"), "rb") as fh:
                self.assertEqual(fh.read(), PAYLOAD)

    async def test_existing_part_uses_single_stream_resume(self):
        with _Srv() as srv, tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "big.bin.part"), "wb") as fh:
                fh.write(PAYLOAD[:1000])
            dm = _manager(srv.base)
            snap = await _run(dm, tmp, url=srv.base + "/f")
            self.assertEqual(snap["files"][0]["status"], "completed")
            self.assertEqual(_Handler.seen_ranges, ["bytes=1000-"])
            with open(os.path.join(tmp, "big.bin"), "rb") as fh:
                self.assertEqual(fh.read(), PAYLOAD)

    async def test_speed_limit_in_snapshot_and_throttles(self):
        with _Srv() as srv, tempfile.TemporaryDirectory() as tmp:
            dm = _manager(srv.base)
            t = time.monotonic()
            snap = await _run(dm, tmp, url=srv.base + "/f", speed_limit_kbps=200)
            self.assertEqual(snap["speed_limit_kbps"], 200)
            self.assertEqual(snap["files"][0]["status"], "completed")
            # 600KB at ~200KB/s with 200KB burst -> >= ~1.5s
            self.assertGreaterEqual(time.monotonic() - t, 1.2)


if __name__ == "__main__":
    unittest.main()
