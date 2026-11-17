"""Comprehensive offline unit and integration test suite for SharePoint Direct Downloader & Sync."""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import os
import re
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import app as web_app  # noqa: E402
import sp_auth  # noqa: E402
from downloader import DownloadManager, MANIFEST_FILENAME  # noqa: E402
from sp_auth import AuthSession, BrowserAuthManager  # noqa: E402
from sp_client import SPItem, SharePointClient  # noqa: E402
from sync_engine import (  # noqa: E402
    build_sync_completion_report,
    compare_sharepoint_with_local,
    export_sync_csv,
    export_sync_json,
)
from utils import (  # noqa: E402
    build_local_path,
    encode_sharing_url,
    encode_sp_path_param,
    extract_sharepoint_url_hints,
    format_bytes,
    sanitize_windows_component,
    win_long_path,
)
from verifier import (  # noqa: E402
    export_verification_csv,
    export_verification_json,
    verify_download,
)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _MockSharePointServer:
    """Offline HTTP server simulating SharePoint APIs, Range resume, 429, and HTML error pages."""

    def __init__(self) -> None:
        self.payload_resume = os.urandom(128 * 1024) + b"TAIL-MARKER-12345"
        self.payload_html_retry = b"%PDF-1.4\nReal Azizi Brochure Content\n%%EOF"
        self.payload_rate_limited = b"Rate limited file content ok"
        self.payload_arabic = b"Arabic path content OK"

        self.resume_hits = 0
        self.html_hits = 0
        self.rate_hits = 0
        self.seen_ranges: list[str] = []
        self.seen_cookies: list[str] = []

        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                return

            def _send_json(self, status: int, obj: dict) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)
                cookie = self.headers.get("Cookie", "")
                if cookie:
                    parent.seen_cookies.append(cookie)

                # --- Mock SharePoint v2.0 APIs (both tenant root and /sites/<site>) ---
                v2_path = re.sub(r"^/sites/[^/]+(?=/_api/v2\.0/)", "", path)
                if v2_path.startswith("/_api/v2.0/shares/") and v2_path.endswith("/driveItem"):
                    self._send_json(
                        200,
                        {
                            "id": "root_item_1",
                            "name": "Azizi Marketing",
                            "folder": {"childCount": 2},
                            "parentReference": {"driveId": "drv_1"},
                        },
                    )
                    return

                if v2_path == "/_api/v2.0/drives/drv_1/items/root_item_1/children":
                    page = qs.get("page", ["1"])[0]
                    if page == "1":
                        self._send_json(
                            200,
                            {
                                "value": [
                                    {
                                        "id": "fld_a",
                                        "name": "Al Furjan",
                                        "folder": {"childCount": 1},
                                        "parentReference": {"driveId": "drv_1"},
                                    }
                                ],
                                "@odata.nextLink": f"{parent.base_url}{path}?page=2",
                            },
                        )
                    else:
                        self._send_json(
                            200,
                            {
                                "value": [
                                    {
                                        "id": "file_root_1",
                                        "name": "Overview.pdf",
                                        "file": {"mimeType": "application/pdf"},
                                        "size": len(parent.payload_rate_limited),
                                        "parentReference": {"driveId": "drv_1"},
                                        "@content.downloadUrl": f"{parent.base_url}/dl/rate",
                                    }
                                ]
                            },
                        )
                    return

                if v2_path == "/_api/v2.0/drives/drv_1/items/fld_a/children":
                    self._send_json(
                        200,
                        {
                            "value": [
                                {
                                    "id": "file_a_1",
                                    "name": "Brochure.pdf",
                                    "file": {"mimeType": "application/pdf"},
                                    "size": len(parent.payload_resume),
                                    "parentReference": {"driveId": "drv_1"},
                                    "@content.downloadUrl": f"{parent.base_url}/dl/resume",
                                }
                            ]
                        },
                    )
                    return

                # --- Mock SharePoint REST v1 APIs ---
                if path == "/sites/Demo/_api/web/GetFolderByServerRelativePath(decodedurl=@p)/Folders":
                    self._send_json(
                        200,
                        {
                            "value": [
                                {"Name": "Forms", "ServerRelativeUrl": "/sites/Demo/Shared Documents/Forms", "UniqueId": "u_forms", "ItemCount": 3},
                                {"Name": "Sub#1", "ServerRelativeUrl": "/sites/Demo/Shared Documents/Sub#1", "UniqueId": "u_sub1", "ItemCount": 1},
                            ]
                        },
                    )
                    return

                if path == "/sites/Demo/_api/web/GetFolderByServerRelativePath(decodedurl=@p)/Files":
                    self._send_json(
                        200,
                        {
                            "value": [
                                {
                                    "Name": "مخطط #1%.pdf",
                                    "ServerRelativeUrl": "/sites/Demo/Shared Documents/مخطط #1%.pdf",
                                    "Length": "2048",
                                    "UniqueId": "u_f1",
                                    "TimeLastModified": "2026-11-10T10:00:00Z",
                                }
                            ]
                        },
                    )
                    return

                # --- Mock Download Endpoints ---
                if path == "/dl/resume":
                    parent.resume_hits += 1
                    rng = self.headers.get("Range")
                    if rng:
                        parent.seen_ranges.append(rng)
                    data = parent.payload_resume
                    total = len(data)
                    if parent.resume_hits == 1 and not rng:
                        # Simulate abrupt TCP drop mid-transfer after sending partial bytes
                        partial = data[:36000]
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(total))
                        self.end_headers()
                        self.wfile.write(partial)
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    if rng:
                        m = re.match(r"bytes=(\d+)-", rng)
                        start = int(m.group(1)) if m else 0
                        chunk = data[start:]
                        self.send_response(206)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
                        self.send_header("Content-Length", str(len(chunk)))
                        self.end_headers()
                        self.wfile.write(chunk)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(total))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                if path == "/dl/html_bad":
                    parent.html_hits += 1
                    body = b"<html><body>Sign in to your Microsoft account</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/dl/html_good":
                    data = parent.payload_html_retry
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                if path == "/dl/rate":
                    parent.rate_hits += 1
                    if parent.rate_hits == 1:
                        self.send_response(429)
                        self.send_header("Retry-After", "0")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    data = parent.payload_rate_limited
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                if path == "/dl/arabic":
                    data = parent.payload_arabic
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                self._send_json(404, {"error": "not found"})

            def do_POST(self):
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)
                if path == "/sites/Demo/_api/contextinfo":
                    self._send_json(200, {"FormDigestValue": "DIGEST-123"})
                    return
                if path.endswith("/RenderListDataAsStream"):
                    if "Paged" not in qs:
                        self._send_json(
                            200,
                            {
                                "Row": [
                                    {
                                        "UniqueId": "r_dir1",
                                        "FileLeafRef": "Drawings",
                                        "FSObjType": "1",
                                        "FileRef": "/sites/Demo/Shared Documents/Drawings",
                                    }
                                ],
                                "NextHref": "?Paged=TRUE&p_ID=2",
                            },
                        )
                    else:
                        self._send_json(
                            200,
                            {
                                "Row": [
                                    {
                                        "UniqueId": "r_file1",
                                        "FileLeafRef": "Plan.dwg",
                                        "FSObjType": "0",
                                        "FileRef": "/sites/Demo/Shared Documents/Drawings/Plan.dwg",
                                        "File_x0020_Size": "4096",
                                        ".spItemUrl": f"{parent.base_url}/_api/v2.0/drives/drv_1/items/it_plan",
                                    }
                                ]
                            },
                        )
                    return
                self._send_json(404, {"error": "not found"})

        self.server = _ThreadedHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()


class TestUtilsAndAuth(unittest.TestCase):
    def test_azizi_sharing_url_hints_and_share_id(self):
        url = (
            "https://aziziinvestment.sharepoint.com/:f:/s/"
            "Azizi_Marketing_Materials/EhnGAtyPTzBEk7COuRZgCq4BMvzHLWIEt7FsOzxdvV3AAw?e=YkZhwg"
        )
        hints = extract_sharepoint_url_hints(url)
        self.assertEqual(hints["origin"], "https://aziziinvestment.sharepoint.com")
        self.assertEqual(hints["web_path"], "/sites/Azizi_Marketing_Materials")
        self.assertEqual(
            hints["web_url"],
            "https://aziziinvestment.sharepoint.com/sites/Azizi_Marketing_Materials",
        )
        self.assertEqual(hints["is_sharing_link"], "1")

        share_id = encode_sharing_url(url)
        self.assertTrue(share_id.startswith("u!"))
        self.assertNotIn("=", share_id)

        # Also check AllItems.aspx?id=... and personal /:f:/g/personal/...
        all_items = (
            "https://aziziinvestment.sharepoint.com/sites/Azizi_Marketing_Materials/"
            "Shared%20Documents/Forms/AllItems.aspx?id=%2Fsites%2FAzizi_Marketing_Materials%2FShared%20Documents%2FAzizi%20Marketing&ga=1"
        )
        h2 = extract_sharepoint_url_hints(all_items)
        self.assertEqual(
            h2["folder_id"],
            "/sites/Azizi_Marketing_Materials/Shared Documents/Azizi Marketing",
        )
        self.assertEqual(h2["web_path"], "/sites/Azizi_Marketing_Materials")

        personal = "https://tenant-my.sharepoint.com/:f:/g/personal/user_azizi_com/Eabc123?e=xyz"
        h3 = extract_sharepoint_url_hints(personal)
        self.assertEqual(h3["web_path"], "/personal/user_azizi_com")

    def test_windows_sanitization_long_paths_and_formatting(self):
        self.assertEqual(sanitize_windows_component("CON"), "_CON")
        self.assertEqual(sanitize_windows_component("ملف عزيزي #1%.pdf"), "ملف عزيزي #1%.pdf")
        self.assertEqual(sanitize_windows_component("bad:name?.pdf "), "bad_name_.pdf")

        with tempfile.TemporaryDirectory() as tmp:
            deep_rel = "/".join(["Azizi_Project_Phase_" + ("X" * 35)] * 8) + "/ملف_التصميم.pdf"
            target = build_local_path(tmp, deep_rel)
            lp = win_long_path(target)
            if os.name == "nt":
                self.assertTrue(lp.startswith("\\\\?\\"))
                self.assertEqual(win_long_path(lp), lp)
                self.assertGreater(len(lp), 260)
                os.makedirs(win_long_path(target.parent), exist_ok=True)
                with open(lp, "wb") as fh:
                    fh.write(b"long-path-ok")
                self.assertTrue(os.path.isfile(lp))

        encoded_p = encode_sp_path_param("/sites/Demo/File #1's.pdf")
        self.assertIn("%23", encoded_p)
        self.assertIn("%27%27", encoded_p)
        self.assertNotIn("#", encoded_p)
        self.assertEqual(format_bytes(1024 * 1024), "1.00 MB")
        self.assertEqual(format_bytes(-1), "غير معروف")

    def test_import_cookies_and_curl(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "session_cache.json"
            profile_dir = Path(tmp) / "browser_profile"
            with mock.patch.object(sp_auth, "SESSION_CACHE_FILE", cache_file), mock.patch.object(
                sp_auth, "PROFILE_DIR", profile_dir
            ):
                mgr = BrowserAuthManager()
                curl_cmd = (
                    'curl ^"https://aziziinvestment.sharepoint.com/sites/Azizi_Marketing_Materials/Shared%20Documents^" '
                    '-H ^"Cookie: FedAuth=abc123; rtFa=xyz789^" '
                    '-H ^"User-Agent: TestBrowser/1.0^"'
                )
                sess = mgr.import_from_curl_or_cookies(curl_cmd, "")
                self.assertTrue(sess.authenticated)
                self.assertTrue(sess.has_sharepoint_auth_cookie())
                self.assertEqual(sess.user_agent, "TestBrowser/1.0")
                hdr = sess.cookie_header_for_url("https://aziziinvestment.sharepoint.com/_api/web")
                self.assertIn("FedAuth=abc123", hdr)
                self.assertIn("rtFa=xyz789", hdr)


class TestSharePointClientAndDownloader(unittest.IsolatedAsyncioTestCase):
    async def test_sp_client_v2_rest_render_and_scan(self):
        with _MockSharePointServer() as srv:
            mgr = BrowserAuthManager()
            mgr.session = AuthSession(
                original_url=f"{srv.base_url}/:f:/s/Azizi_Marketing_Materials/Token?e=1",
                resolved_url=f"{srv.base_url}/:f:/s/Azizi_Marketing_Materials/Token?e=1",
                origin=srv.base_url,
                web_url=srv.base_url,
                cookies=[{"name": "FedAuth", "value": "cookie1", "domain": "127.0.0.1", "path": "/"}],
                authenticated=True,
            )
            client = SharePointClient(mgr)
            root = await client.resolve_root()
            self.assertEqual(root.backend, "v2")
            self.assertEqual(root.name, "Azizi Marketing")

            children = await client.list_children(root)
            self.assertEqual([c.name for c in children], ["Al Furjan", "Overview.pdf"])

            # Scan selecting root + duplicate subfolder -> deduplicated
            al_furjan = children[0]
            manifest = await client.scan_selected_items(
                [root.to_dict(), al_furjan.to_dict()]
            )
            self.assertEqual(manifest["total_files"], 2)
            self.assertEqual(
                [f["relative_path"] for f in manifest["files"]],
                ["Al Furjan/Brochure.pdf", "Overview.pdf"],
            )

            # REST v1 listing (handles string Length, #/%/Arabic, and skips Forms at library root)
            mgr.session.web_url = f"{srv.base_url}/sites/Demo"
            mgr.session.list_url = "/sites/Demo/Shared Documents"
            rest_root = SPItem(
                id="rest_root",
                name="Shared Documents",
                is_folder=True,
                relative_path="",
                server_relative_path="/sites/Demo/Shared Documents",
                backend="rest_v1",
            )
            rest_children = await client.list_children(rest_root)
            names = [c.name for c in rest_children]
            self.assertNotIn("Forms", names)
            self.assertIn("Sub#1", names)
            self.assertIn("مخطط #1%.pdf", names)
            arabic_file = next(c for c in rest_children if c.name == "مخطط #1%.pdf")
            self.assertEqual(arabic_file.size, 2048)

            # RenderListDataAsStream with NextHref pagination
            mgr.session.list_id = "guid-1"
            render_root = SPItem(
                id="rnd_root",
                name="Shared Documents",
                is_folder=True,
                relative_path="",
                server_relative_path="/sites/Demo/Shared Documents",
                backend="render_list",
            )
            rnd_children = await client.list_children(render_root)
            self.assertEqual([c.name for c in rnd_children], ["Drawings", "Plan.dwg"])
            self.assertEqual(rnd_children[1].size, 4096)
            self.assertEqual(rnd_children[1].drive_id, "drv_1")
            self.assertEqual(rnd_children[1].item_id, "it_plan")

    async def test_downloader_resume_html_rejection_429_and_long_paths(self):
        with _MockSharePointServer() as srv, tempfile.TemporaryDirectory() as tmp:
            mgr = BrowserAuthManager()
            mgr.session = AuthSession(
                origin=srv.base_url,
                web_url=srv.base_url,
                cookies=[{"name": "FedAuth", "value": "ok", "domain": "127.0.0.1", "path": "/"}],
                authenticated=True,
            )
            client = SharePointClient(mgr)

            # Swap refresh_download_url for the HTML-error test file to return valid PDF URL on retry
            orig_refresh = client.refresh_download_url

            async def mock_refresh(file_entry):
                if file_entry.get("name") == "HTMLProtected.pdf":
                    return f"{srv.base_url}/dl/html_good", []
                return await orig_refresh(file_entry)

            client.refresh_download_url = mock_refresh  # type: ignore[method-assign]

            dm = DownloadManager(client)
            deep_folder = "/".join(["مجلد_مشروع_عزيزي_" + ("A" * 35)] * 7)
            already_path = build_local_path(tmp, "AlreadyDone.txt")
            os.makedirs(win_long_path(already_path.parent), exist_ok=True)
            with open(win_long_path(already_path), "wb") as fh:
                fh.write(b"123456")

            manifest = {
                "folders": ["EmptyFolder/NestedEmpty", deep_folder],
                "files": [
                    {
                        "name": "Brochure.pdf",
                        "relative_path": "Al Furjan/Brochure.pdf",
                        "size": len(srv.payload_resume),
                        "download_url": f"{srv.base_url}/dl/resume",
                    },
                    {
                        "name": "HTMLProtected.pdf",
                        "relative_path": "HTMLProtected.pdf",
                        "size": len(srv.payload_html_retry),
                        "download_url": f"{srv.base_url}/dl/html_bad",
                    },
                    {
                        "name": "Overview.pdf",
                        "relative_path": "Overview.pdf",
                        "size": len(srv.payload_rate_limited),
                        "download_url": f"{srv.base_url}/dl/rate",
                    },
                    {
                        "name": "ملف_طويل.pdf",
                        "relative_path": f"{deep_folder}/ملف_طويل.pdf",
                        "size": len(srv.payload_arabic),
                        "download_url": f"{srv.base_url}/dl/arabic",
                    },
                    {
                        "name": "AlreadyDone.txt",
                        "relative_path": "AlreadyDone.txt",
                        "size": 6,
                        "download_url": f"{srv.base_url}/dl/nonexistent",
                    },
                ],
            }

            await dm.start_job(
                dest_dir=tmp,
                manifest=manifest,
                concurrency=2,
                max_retries=4,
                skip_verified=True,
                create_empty_folders=True,
            )

            # Wait for job completion
            deadline = time.monotonic() + 15.0
            while dm.get_snapshot()["status"] not in ("completed", "cancelled"):
                if time.monotonic() > deadline:
                    self.fail("DownloadManager timed out")
                await asyncio.sleep(0.05)

            snap = dm.get_snapshot()
            self.assertEqual(snap["status"], "completed")
            self.assertEqual(snap["completed_files"], 4)
            self.assertEqual(snap["skipped_files"], 1)
            self.assertEqual(snap["failed_files"], 0)
            self.assertTrue(any(r.startswith("bytes=36000-") for r in srv.seen_ranges))
            self.assertGreaterEqual(srv.html_hits, 1)
            self.assertGreaterEqual(srv.rate_hits, 2)

            # Verify exact byte-for-byte integrity via SHA-256
            brochure_local = build_local_path(tmp, "Al Furjan/Brochure.pdf")
            with open(win_long_path(brochure_local), "rb") as fh:
                got = fh.read()
            self.assertEqual(
                hashlib.sha256(got).hexdigest(),
                hashlib.sha256(srv.payload_resume).hexdigest(),
            )

            html_local = build_local_path(tmp, "HTMLProtected.pdf")
            with open(win_long_path(html_local), "rb") as fh:
                self.assertEqual(fh.read(), srv.payload_html_retry)

            # Empty folder and deep Windows long path preserved
            empty_dir = build_local_path(tmp, "EmptyFolder/NestedEmpty")
            self.assertTrue(os.path.isdir(win_long_path(empty_dir)))
            long_file = build_local_path(tmp, f"{deep_folder}/ملف_طويل.pdf")
            self.assertTrue(os.path.isfile(win_long_path(long_file)))
            self.assertTrue(os.path.isfile(win_long_path(Path(tmp) / MANIFEST_FILENAME)))


class TestVerifierAndSyncEngine(unittest.TestCase):
    def test_verifier_states_and_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(Path(tmp) / "FolderA", exist_ok=True)
            (Path(tmp) / "FolderA" / "ok.bin").write_bytes(b"12345")
            (Path(tmp) / "FolderA" / "wrong.bin").write_bytes(b"12")
            (Path(tmp) / "FolderA" / "partial.bin.part").write_bytes(b"123")

            manifest = {
                "folders": ["FolderA", "MissingFolder"],
                "files": [
                    {"name": "ok.bin", "relative_path": "FolderA/ok.bin", "size": 5},
                    {"name": "wrong.bin", "relative_path": "FolderA/wrong.bin", "size": 10},
                    {"name": "partial.bin", "relative_path": "FolderA/partial.bin", "size": 10},
                    {"name": "gone.bin", "relative_path": "MissingFolder/gone.bin", "size": 8},
                ],
            }
            rep = verify_download(manifest, tmp)
            self.assertFalse(rep["all_ok"])
            self.assertEqual(rep["verified_files"], 1)
            self.assertEqual(rep["size_mismatch_files"], 1)
            self.assertEqual(rep["incomplete_part_files"], 1)
            self.assertEqual(rep["missing_files"], 1)
            self.assertEqual(rep["missing_folders"], 1)
            self.assertEqual(len(rep["retry_manifest"]["files"]), 3)

            csv_text = export_verification_csv(rep)
            self.assertTrue(csv_text.startswith("\ufeff"))
            self.assertIn("MissingFolder/gone.bin", csv_text)
            self.assertIn("MissingFolder", export_verification_json(rep))

    def test_sync_compare_and_completion_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Existing").mkdir(parents=True)
            (Path(tmp) / "Existing" / "same.pdf").write_bytes(b"ABCD")
            (Path(tmp) / "Existing" / "changed.pdf").write_bytes(b"OLD")
            (Path(tmp) / "LocalOnly.txt").write_bytes(b"keep-me")

            sp_manifest = {
                "folders": ["Existing", "NewPhase/Sub"],
                "files": [
                    {"name": "same.pdf", "relative_path": "Existing/same.pdf", "size": 4},
                    {"name": "changed.pdf", "relative_path": "Existing/changed.pdf", "size": 9},
                    {"name": "added_in_existing.pdf", "relative_path": "Existing/added_in_existing.pdf", "size": 100},
                    {"name": "fresh.dwg", "relative_path": "NewPhase/Sub/fresh.dwg", "size": 500},
                ],
            }

            plan = compare_sharepoint_with_local(sp_manifest, tmp)
            self.assertEqual(plan["summary"]["new_files_count"], 2)
            self.assertEqual(plan["summary"]["updated_files_count"], 1)
            self.assertEqual(plan["summary"]["up_to_date_files_count"], 1)
            self.assertEqual(plan["summary"]["local_only_files_count"], 1)
            self.assertEqual(plan["summary"]["new_folders_count"], 1)

            folder_map = {b["folder"]: b for b in plan["folder_summary"]}
            self.assertIn("Existing", folder_map)
            self.assertEqual(folder_map["Existing"]["new_file_names"], ["added_in_existing.pdf"])
            self.assertEqual(folder_map["Existing"]["updated_file_names"], ["changed.pdf"])
            self.assertEqual(folder_map["NewPhase/Sub"]["new_file_names"], ["fresh.dwg"])

            fake_snapshot = {
                "job_id": "job999",
                "mode": "sync",
                "status": "completed",
                "dest_dir": tmp,
                "files": [
                    {
                        "relative_path": "Existing/added_in_existing.pdf",
                        "name": "added_in_existing.pdf",
                        "size": 100,
                        "downloaded_bytes": 100,
                        "status": "completed",
                        "sync_reason": "new",
                        "attempts": 1,
                        "error": None,
                    },
                    {
                        "relative_path": "NewPhase/Sub/fresh.dwg",
                        "name": "fresh.dwg",
                        "size": 500,
                        "downloaded_bytes": 500,
                        "status": "completed",
                        "sync_reason": "new",
                        "attempts": 1,
                        "error": None,
                    },
                ],
            }
            rep = build_sync_completion_report(plan, fake_snapshot, tmp)
            self.assertEqual(rep["newly_downloaded_count"], 2)
            self.assertEqual(rep["already_up_to_date_count"], 1)
            self.assertEqual(rep["local_only_count"], 1)
            by_folder = {g["folder"]: g["file_names"] for g in rep["downloaded_by_folder"]}
            self.assertEqual(by_folder["Existing"], ["added_in_existing.pdf"])
            self.assertEqual(by_folder["NewPhase/Sub"], ["fresh.dwg"])
            self.assertTrue(export_sync_csv(rep).startswith("\ufeff"))
            self.assertIn("fresh.dwg", export_sync_json(rep))


class TestWebApiIntegration(unittest.TestCase):
    def test_starlette_endpoints_and_sync_flow(self):
        with _MockSharePointServer() as srv, tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "_cache" / "session_cache.json"
            profile_dir = Path(tmp) / "_cache" / "browser_profile"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            dest_dir = Path(tmp) / "dest"
            dest_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(sp_auth, "SESSION_CACHE_FILE", cache_file), mock.patch.object(
                sp_auth, "PROFILE_DIR", profile_dir
            ):
                fresh_state = web_app.AppState()
                with mock.patch.object(web_app, "state", fresh_state):
                    # Enter the client so ONE event loop/portal lives for the whole test.
                    # Without it each request gets its own loop and the background
                    # download workers/queue (bound to the first request's loop) are
                    # orphaned, which hangs /api/sync/download.
                    client = TestClient(web_app.app)
                    client.__enter__()
                    self.addCleanup(client.__exit__, None, None, None)

                    r_idx = client.get("/")
                    self.assertEqual(r_idx.status_code, 200)
                    self.assertIn("SharePoint", r_idx.text)

                    self.assertEqual(client.get("/api/health").json(), {"ok": True, "version": "5.0.1"})

                    r_imp = client.post(
                        "/api/auth/import-cookies",
                        json={
                            "url": f"{srv.base_url}/:f:/s/Azizi_Marketing_Materials/Token?e=1",
                            "raw_text": "FedAuth=test_cookie; rtFa=rt_cookie",
                        },
                    )
                    self.assertEqual(r_imp.status_code, 200)
                    self.assertTrue(r_imp.json()["session"]["authenticated"])

                    r_root = client.get("/api/sp/root")
                    self.assertEqual(r_root.status_code, 200)
                    root_item = r_root.json()["root"]
                    self.assertEqual(root_item["name"], "Azizi Marketing")

                    r_scan = client.post(
                        "/api/sp/scan",
                        json={
                            "selected_items": [root_item],
                            "include_root_name": True,
                            "root_name": root_item["name"],
                        },
                    )
                    self.assertEqual(r_scan.status_code, 200)
                    manifest = r_scan.json()["manifest"]
                    self.assertEqual(manifest["total_files"], 2)
                    self.assertTrue(
                        all(f["relative_path"].startswith("Azizi Marketing/") for f in manifest["files"])
                    )

                    # Verify SSE endpoint emits initial snapshot event cleanly
                    sse_q = fresh_state.download_manager.subscribe()
                    try:
                        first_snap = sse_q.get_nowait()
                        self.assertEqual(first_snap["status"], "idle")
                    finally:
                        fresh_state.download_manager.unsubscribe(sse_q)

                    # Sync compare against empty local dir -> file should appear as new
                    r_cmp = client.post(
                        "/api/sync/compare",
                        json={
                            "local_dir": str(dest_dir),
                            "manifest": {
                                "folders": ["Azizi Marketing/Al Furjan"],
                                "files": [
                                    {
                                        "name": "Overview.pdf",
                                        "relative_path": "Overview.pdf",
                                        "size": len(srv.payload_rate_limited),
                                        "download_url": f"{srv.base_url}/dl/rate",
                                    }
                                ],
                            },
                            "include_root_name": True,
                            "root_name": "Azizi Marketing",
                        },
                    )
                    self.assertEqual(r_cmp.status_code, 200)
                    self.assertEqual(r_cmp.json()["summary"]["new_files_count"], 1)

                    r_dl = client.post(
                        "/api/sync/download",
                        json={"local_dir": str(dest_dir), "concurrency": 1, "max_retries": 3},
                    )
                    self.assertEqual(r_dl.status_code, 200)
                    self.assertTrue(r_dl.json()["started"])

                    deadline = time.monotonic() + 10.0
                    while client.get("/api/download/status").json()["status"] != "completed":
                        if time.monotonic() > deadline:
                            self.fail("API sync download did not complete in time")
                        time.sleep(0.05)

                    r_rep = client.get("/api/sync/report")
                    self.assertEqual(r_rep.status_code, 200)
                    self.assertEqual(r_rep.json()["newly_downloaded_count"], 1)

                    r_ver = client.post("/api/verify", json={"dest_dir": str(dest_dir)})
                    self.assertEqual(r_ver.status_code, 200)
                    self.assertTrue(r_ver.json()["all_ok"])

                    r_csv = client.get("/api/verify/export?format=csv")
                    self.assertEqual(r_csv.status_code, 200)
                    self.assertIn("Overview.pdf", r_csv.text)

                    r_scsv = client.get("/api/sync/export?format=csv&scope=report")
                    self.assertEqual(r_scsv.status_code, 200)
                    self.assertIn("Overview.pdf", r_scsv.text)

                    # Verify an independent directory uses its own saved manifest rather than sync state
                    other_dir = Path(tmp) / "other_dir"
                    other_dir.mkdir(parents=True, exist_ok=True)
                    (other_dir / "Saved.txt").write_bytes(b"abc")
                    (other_dir / MANIFEST_FILENAME).write_text(
                        json.dumps(
                            {
                                "folders": [],
                                "files": [{"name": "Saved.txt", "relative_path": "Saved.txt", "size": 3}],
                            }
                        ),
                        encoding="utf-8",
                    )
                    r_other = client.post("/api/verify", json={"dest_dir": str(other_dir)})
                    self.assertEqual(r_other.status_code, 200)
                    self.assertEqual(r_other.json()["total_files"], 1)
                    self.assertTrue(r_other.json()["all_ok"])

                    # Verify SSE endpoint event generator emits initial snapshot & cleans up
                    async def _check_sse() -> None:
                        calls = 0

                        class _FakeRequest:
                            async def is_disconnected(self) -> bool:
                                nonlocal calls
                                calls += 1
                                return calls > 1

                        resp = await web_app.api_download_events(_FakeRequest())  # type: ignore[arg-type]
                        gen = resp.body_iterator
                        first_ev = await gen.__anext__()
                        self.assertEqual(first_ev["event"], "snapshot")
                        parsed = json.loads(first_ev["data"])
                        # SSE multiplexes media_manager (idle) and download_manager
                        # snapshots; the first one may be the media manager's idle.
                        self.assertIn(parsed["status"], ("idle", "completed"))
                        self.assertEqual(fresh_state.download_manager.get_snapshot()["status"], "completed")
                        with self.assertRaises(StopAsyncIteration):
                            await gen.__anext__()
                        self.assertEqual(len(fresh_state.download_manager._subscribers), 0)

                    asyncio.run(_check_sse())


if __name__ == "__main__":
    unittest.main(verbosity=2)
