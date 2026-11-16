"""Starlette + Uvicorn web server for SharePoint Direct Downloader & Sync."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from sse_starlette import EventSourceResponse

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from downloader import DownloadManager, MANIFEST_FILENAME  # noqa: E402
from sp_auth import BrowserAuthManager  # noqa: E402
from sp_client import SPItem, SharePointClient  # noqa: E402
from sync_engine import (  # noqa: E402
    DiskSyncManager,
    build_sync_completion_report,
    compare_sharepoint_with_local,
    compare_two_local_directories,
    export_sync_csv,
    export_sync_json,
)
from utils import extract_sharepoint_url_hints, win_long_path  # noqa: E402
from verifier import (  # noqa: E402
    export_verification_csv,
    export_verification_json,
    verify_download,
)

STATIC_DIR = BASE_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"


class AppState:
    """In-memory coordinator for auth, SharePoint exploration, downloads, verification, and sync."""

    def __init__(self) -> None:
        self.auth_manager = BrowserAuthManager()
        self.sp_client = SharePointClient(self.auth_manager)
        self.download_manager = DownloadManager(self.sp_client)
        self.current_manifest: Optional[dict[str, Any]] = None
        self.current_verification_report: Optional[dict[str, Any]] = None
        self.current_sync_plan: Optional[dict[str, Any]] = None
        self.disk_sync_manager = DiskSyncManager()
        self.current_disk_compare_result: Optional[dict[str, Any]] = None
        self.scan_lock = asyncio.Lock()
        self.scan_state: dict[str, Any] = self._empty_scan_state()

    @staticmethod
    def _empty_scan_state() -> dict[str, Any]:
        return {
            "status": "idle",
            "scanned_folders": 0,
            "found_files": 0,
            "total_bytes": 0,
            "current_folder": "",
            "error": None,
            "started_at": None,
            "finished_at": None,
        }


state = AppState()


async def _read_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("صيغة JSON غير صالحة") from exc
    if not isinstance(parsed, dict):
        raise ValueError("يجب أن يكون محتوى الطلب كائن JSON")
    return parsed


def _apply_root_prefix(
    manifest: dict[str, Any],
    include_root_name: bool,
    root_name: Optional[str],
) -> dict[str, Any]:
    """Prefix folder and file `relative_path` values with `<root_name>/` when enabled."""
    if not include_root_name or not root_name:
        return manifest
    prefix = str(root_name).replace("\\", "/").strip("/")
    if not prefix:
        return manifest
    prefix_slash = f"{prefix}/"

    def _prefix_path(rel: str) -> str:
        cleaned = (rel or "").replace("\\", "/").strip("/")
        if not cleaned:
            return prefix
        if cleaned == prefix or cleaned.startswith(prefix_slash):
            return cleaned
        return f"{prefix}/{cleaned}"

    folders_map: dict[str, str] = {prefix: prefix}
    for folder in manifest.get("folders") or []:
        prefixed = _prefix_path(str(folder))
        if prefixed:
            folders_map[prefixed] = prefixed

    updated_files: list[dict[str, Any]] = []
    for raw_file in manifest.get("files") or []:
        item = dict(raw_file)
        orig_rel = item.get("relative_path") or item.get("name") or ""
        item["relative_path"] = _prefix_path(str(orig_rel))
        updated_files.append(item)

    ordered_folders = sorted(folders_map.keys())
    out = dict(manifest)
    out["folders"] = ordered_folders
    out["files"] = updated_files
    out["total_folders"] = len(ordered_folders)
    out["total_files"] = len(updated_files)
    return out


def _sp_item_from_dict(raw: dict[str, Any]) -> SPItem:
    raw_size = raw.get("size", 0)
    try:
        size = int(raw_size) if raw_size is not None else 0
    except (TypeError, ValueError):
        size = 0
    return SPItem(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        is_folder=bool(raw.get("is_folder", True)),
        size=size,
        relative_path=str(raw.get("relative_path") or "").strip("/"),
        server_relative_path=raw.get("server_relative_path"),
        drive_id=raw.get("drive_id"),
        item_id=raw.get("item_id"),
        download_url=raw.get("download_url"),
        fallback_download_urls=list(raw.get("fallback_download_urls") or []),
        last_modified=str(raw.get("last_modified") or ""),
        child_count=raw.get("child_count"),
        backend=str(raw.get("backend") or "v2"),
    )


async def _scan_with_progress(
    selected_items: list[dict[str, Any]],
    include_root_name: bool,
    root_name: Optional[str],
) -> dict[str, Any]:
    async with state.scan_lock:
        state.scan_state = {
            "status": "scanning",
            "scanned_folders": 0,
            "found_files": 0,
            "total_bytes": 0,
            "current_folder": "",
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
        }

        def _on_progress(
            scanned_folders: int,
            found_files: int,
            total_bytes: int,
            current_folder: str,
        ) -> None:
            state.scan_state["scanned_folders"] = scanned_folders
            state.scan_state["found_files"] = found_files
            state.scan_state["total_bytes"] = total_bytes
            state.scan_state["current_folder"] = current_folder

        try:
            manifest = await state.sp_client.scan_selected_items(
                selected_items,
                on_progress=_on_progress,
            )
            manifest = _apply_root_prefix(manifest, include_root_name, root_name)
            state.current_manifest = manifest
            state.scan_state["status"] = "completed"
            state.scan_state["scanned_folders"] = max(
                state.scan_state["scanned_folders"],
                int(manifest.get("total_folders") or 0),
            )
            state.scan_state["found_files"] = int(manifest.get("total_files") or 0)
            state.scan_state["total_bytes"] = int(manifest.get("total_bytes") or 0)
            state.scan_state["finished_at"] = time.time()
            return manifest
        except Exception as exc:
            state.scan_state["status"] = "error"
            state.scan_state["error"] = str(exc)
            state.scan_state["finished_at"] = time.time()
            raise


def _load_saved_manifest(dest_dir: str) -> Optional[dict[str, Any]]:
    manifest_path = Path(dest_dir) / MANIFEST_FILENAME
    lp_manifest = win_long_path(manifest_path)
    if not os.path.isfile(lp_manifest):
        return None
    with open(lp_manifest, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Routes: UI & Health
# ---------------------------------------------------------------------------


async def index_page(request: Request) -> Response:
    if not INDEX_HTML.is_file():
        return JSONResponse({"error": "ملف واجهة المستخدم static/index.html غير موجود"}, status_code=404)
    return FileResponse(
        INDEX_HTML,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def favicon_page(request: Request) -> Response:
    fav = STATIC_DIR / "favicon.svg"
    if fav.is_file():
        return FileResponse(fav, media_type="image/svg+xml")
    return Response(status_code=404)


async def api_health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Routes: Auth
# ---------------------------------------------------------------------------


async def api_auth_open_browser(request: Request) -> JSONResponse:
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    url = str(body.get("url") or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse({"error": "يرجى إدخال رابط SharePoint صحيح يبدأ بـ https://"}, status_code=400)

    try:
        session = await state.auth_manager.start_interactive_login(url)
        return JSONResponse(
            {
                "ok": True,
                "browser_open": state.auth_manager.browser_open,
                "session": session.to_public_dict(),
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_auth_status(request: Request) -> JSONResponse:
    session = await state.auth_manager.sync_and_get_session()
    return JSONResponse(
        {
            "ok": True,
            "browser_open": state.auth_manager.browser_open,
            "session": session.to_public_dict(),
        }
    )


async def api_auth_close_browser(request: Request) -> JSONResponse:
    session = await state.auth_manager.close_browser()
    return JSONResponse(
        {
            "ok": True,
            "browser_open": state.auth_manager.browser_open,
            "session": session.to_public_dict(),
        }
    )


async def api_auth_import_cookies(request: Request) -> JSONResponse:
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    url = str(body.get("url") or state.auth_manager.session.original_url or "").strip()
    raw_text = str(body.get("raw_text") or "").strip()
    try:
        session = state.auth_manager.import_from_curl_or_cookies(
            raw_text=raw_text,
            sharepoint_url=url,
        )
        return JSONResponse(
            {
                "ok": True,
                "browser_open": state.auth_manager.browser_open,
                "session": session.to_public_dict(),
            }
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_auth_clear(request: Request) -> JSONResponse:
    await state.auth_manager.clear_session()
    state.current_manifest = None
    state.current_sync_plan = None
    state.scan_state = state._empty_scan_state()
    return JSONResponse(
        {
            "ok": True,
            "browser_open": False,
            "session": state.auth_manager.session.to_public_dict(),
        }
    )


# ---------------------------------------------------------------------------
# Routes: System Lifecycle (Heartbeat & Auto-Shutdown)
# ---------------------------------------------------------------------------

_last_heartbeat_time: float = time.time()
_heartbeat_active: bool = False
_pending_shutdown: bool = False
_shutdown_deadline: float = 0.0


async def api_system_heartbeat(request: Request) -> JSONResponse:
    global _last_heartbeat_time, _heartbeat_active, _pending_shutdown
    _last_heartbeat_time = time.time()
    _heartbeat_active = True
    _pending_shutdown = False
    return JSONResponse({"status": "ok", "time": _last_heartbeat_time})


async def api_system_client_leaving(request: Request) -> JSONResponse:
    """Triggered on pagehide / beforeunload. Gives a 4-second grace period in case of F5 refresh."""
    global _pending_shutdown, _shutdown_deadline
    if _heartbeat_active:
        _pending_shutdown = True
        _shutdown_deadline = time.time() + 4.0
    return JSONResponse({"status": "acknowledged"})


async def api_system_shutdown(request: Request) -> JSONResponse:
    """Gracefully terminate the server process."""
    def _delayed_exit():
        time.sleep(0.3)
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return JSONResponse({"status": "shutting_down", "message": "تم إيقاف البرنامج بنجاح"})


def _start_heartbeat_watchdog() -> None:
    def _watchdog_loop():
        time.sleep(25)  # Initial grace period for browser to launch and connect
        while True:
            time.sleep(1.5)
            now = time.time()
            if _pending_shutdown and now >= _shutdown_deadline:
                print("OmniSync: Browser window closed. Shutting down background service...")
                os._exit(0)
            if _heartbeat_active and (now - _last_heartbeat_time) > 8.0:
                print("OmniSync: Heartbeat timeout (window closed). Shutting down background service...")
                os._exit(0)

    t = threading.Thread(target=_watchdog_loop, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Routes: System
# ---------------------------------------------------------------------------


async def api_system_pick_folder(request: Request) -> JSONResponse:
    initial_dir = ""
    try:
        body = await _read_json_body(request)
        candidate = str(body.get("initial_dir") or "").strip()
        if candidate and os.path.isdir(win_long_path(candidate)):
            initial_dir = str(Path(candidate).resolve())
    except ValueError:
        pass

    script = (
        "import json, sys, tkinter as tk\n"
        "from pathlib import Path\n"
        "from tkinter import filedialog\n"
        "init_dir = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else ''\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "try:\n"
        "    root.attributes('-topmost', True)\n"
        "except Exception:\n"
        "    pass\n"
        "root.update()\n"
        "opts = {'title': 'اختر مجلد الحفظ / المزامنة', 'mustexist': False}\n"
        "if init_dir:\n"
        "    opts['initialdir'] = init_dir\n"
        "selected = filedialog.askdirectory(parent=root, **opts)\n"
        "root.destroy()\n"
        "out = str(Path(selected)) if selected else ''\n"
        "print(json.dumps({'path': out}, ensure_ascii=False))\n"
    )
    kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            initial_dir,
            **kwargs,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return JSONResponse({"error": f"تعذر فتح نافذة اختيار المجلد: {err}"}, status_code=500)
        raw = stdout.decode("utf-8", errors="replace").strip()
        data = json.loads(raw) if raw else {"path": ""}
        return JSONResponse({"path": str(data.get("path") or "")})
    except Exception as exc:
        return JSONResponse({"error": f"تعذر فتح نافذة اختيار المجلد: {exc}"}, status_code=500)


# ---------------------------------------------------------------------------
# Routes: SharePoint Explorer
# ---------------------------------------------------------------------------


async def api_sp_set_url(request: Request) -> JSONResponse:
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    new_url = str(body.get("url") or "").strip()
    if not new_url or not (new_url.startswith("http://") or new_url.startswith("https://")):
        return JSONResponse({"error": "يرجى إدخال رابط SharePoint صحيح يبدأ بـ https://"}, status_code=400)

    s = state.auth_manager.session
    old_host = (urlparse(s.original_url or s.origin or "").hostname or "").lower()
    new_host = (urlparse(new_url).hostname or "").lower()
    same_host = bool(old_host and new_host and old_host == new_host)

    hints = extract_sharepoint_url_hints(new_url)
    s.original_url = new_url
    s.resolved_url = new_url
    s.origin = hints["origin"] or ""
    s.web_url = hints["web_url"] or ""
    s.web_server_relative = hints["web_path"] or ""
    s.list_id = None
    s.list_url = None
    s.root_folder_server_relative = hints["folder_id"]
    s.drive_id = None
    s.root_item_id = None
    s.root_name = None
    s.form_digest = None
    s.detected_apis = []

    if old_host and not same_host:
        s.cookies = []
        s.authorization_header = None
        s.authenticated = False
        s.status_message = "تم تغيير الموقع — يرجى تسجيل الدخول للرابط الجديد"
    elif s.cookies or s.authorization_header:
        if s.authenticated:
            s.status_message = "تم تحديث الرابط مع الاحتفاظ بجلسة الدخول الحالية"

    state.current_manifest = None
    state.current_sync_plan = None
    state.scan_state = state._empty_scan_state()
    state.auth_manager._save_cached_session()

    return JSONResponse(
        {
            "ok": True,
            "same_host": same_host,
            "session": s.to_public_dict(),
        }
    )


async def api_sp_root(request: Request) -> JSONResponse:
    try:
        root_item = await state.sp_client.resolve_root()
        state.auth_manager._save_cached_session()
        return JSONResponse(
            {
                "ok": True,
                "root": root_item.to_dict(),
                "session": state.auth_manager.session.to_public_dict(),
            }
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_sp_children(request: Request) -> JSONResponse:
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    node = body.get("node") if isinstance(body.get("node"), dict) else body
    if not isinstance(node, dict) or not node.get("name"):
        return JSONResponse({"error": "بيانات المجلد المطلوب غير صالحة"}, status_code=400)

    try:
        folder = _sp_item_from_dict(node)
        children = await state.sp_client.list_children(folder)
        return JSONResponse({"ok": True, "children": [c.to_dict() for c in children]})
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_sp_scan(request: Request) -> JSONResponse:
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    selected_items = body.get("selected_items")
    if not isinstance(selected_items, list) or not selected_items:
        return JSONResponse({"error": "يرجى تحديد مجلد أو ملف واحد على الأقل قبل الفحص"}, status_code=400)

    include_root_name = bool(body.get("include_root_name", False))
    root_name = body.get("root_name") or state.auth_manager.session.root_name

    try:
        manifest = await _scan_with_progress(
            selected_items=selected_items,
            include_root_name=include_root_name,
            root_name=str(root_name) if root_name else None,
        )
        return JSONResponse(
            {
                "ok": True,
                "manifest": manifest,
                "scan_status": dict(state.scan_state),
            }
        )
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_sp_scan_status(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, **state.scan_state})


# ---------------------------------------------------------------------------
# Routes: Download Manager & SSE
# ---------------------------------------------------------------------------


async def api_download_start(request: Request) -> JSONResponse:
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    dest_dir = str(body.get("dest_dir") or "").strip()
    if not dest_dir:
        return JSONResponse({"error": "يرجى تحديد مجلد الحفظ المحلي أولاً"}, status_code=400)
    if os.path.isfile(win_long_path(dest_dir)):
        return JSONResponse({"error": "المسار المحدد يشير إلى ملف وليس مجلداً"}, status_code=400)

    manifest = body.get("manifest") if isinstance(body.get("manifest"), dict) else state.current_manifest
    if not isinstance(manifest, dict) or (
        not manifest.get("files") and not manifest.get("folders")
    ):
        return JSONResponse({"error": "لا توجد قائمة ملفات ممسوحة للتحميل. قم بفحص المجلدات أولاً."}, status_code=400)

    try:
        concurrency = max(1, min(16, int(body.get("concurrency", 3))))
        max_retries = max(0, min(50, int(body.get("max_retries", 10))))
    except (TypeError, ValueError):
        return JSONResponse({"error": "قيم التوازي أو المحاولات غير صالحة"}, status_code=400)

    skip_verified = bool(body.get("skip_verified", True))
    create_empty_folders = bool(body.get("create_empty_folders", True))
    mode = str(body.get("mode") or "download")

    try:
        snapshot = await state.download_manager.start_job(
            dest_dir=dest_dir,
            manifest=manifest,
            concurrency=concurrency,
            max_retries=max_retries,
            skip_verified=skip_verified,
            create_empty_folders=create_empty_folders,
            mode=mode,
        )
        return JSONResponse(
            {
                "started": True,
                "job_id": snapshot.get("job_id"),
                "snapshot": snapshot,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_download_pause(request: Request) -> JSONResponse:
    await state.download_manager.pause()
    return JSONResponse({"ok": True, "snapshot": state.download_manager.get_snapshot()})


async def api_download_resume(request: Request) -> JSONResponse:
    await state.download_manager.resume()
    return JSONResponse({"ok": True, "snapshot": state.download_manager.get_snapshot()})


async def api_download_cancel(request: Request) -> JSONResponse:
    await state.download_manager.cancel()
    return JSONResponse({"ok": True, "snapshot": state.download_manager.get_snapshot()})


async def api_download_retry_failed(request: Request) -> JSONResponse:
    await state.download_manager.retry_failed()
    return JSONResponse({"ok": True, "snapshot": state.download_manager.get_snapshot()})


async def api_download_status(request: Request) -> JSONResponse:
    return JSONResponse(state.download_manager.get_snapshot())


async def api_download_events(request: Request) -> EventSourceResponse:
    queue = state.download_manager.subscribe()

    async def event_generator():
        last_idle_emit = 0.0
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snap = await asyncio.wait_for(queue.get(), timeout=0.4)
                    yield {
                        "event": "snapshot",
                        "data": json.dumps(snap, ensure_ascii=False),
                    }
                    last_idle_emit = time.monotonic()
                except asyncio.TimeoutError:
                    snap = state.download_manager.get_snapshot()
                    now = time.monotonic()
                    if snap.get("status") == "running" or (now - last_idle_emit) >= 4.0:
                        yield {
                            "event": "snapshot",
                            "data": json.dumps(snap, ensure_ascii=False),
                        }
                        last_idle_emit = now
        except asyncio.CancelledError:
            return
        finally:
            state.download_manager.unsubscribe(queue)

    return EventSourceResponse(
        event_generator(),
        ping=15,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Routes: Verification
# ---------------------------------------------------------------------------


async def api_verify(request: Request) -> JSONResponse:
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    last_dest = state.download_manager.get_snapshot().get("dest_dir") or ""
    dest_dir = str(body.get("dest_dir") or last_dest or "").strip()
    if not dest_dir:
        return JSONResponse({"error": "يرجى تحديد مجلد التحقق المحلي"}, status_code=400)

    resolved_dest = str(Path(dest_dir).resolve())
    saved_manifest: Optional[dict[str, Any]] = None
    try:
        saved_manifest = await asyncio.to_thread(_load_saved_manifest, dest_dir)
    except Exception as exc:
        return JSONResponse({"error": f"تعذر قراءة ملف الفحص المحفوظ: {exc}"}, status_code=500)

    snap = state.download_manager.get_snapshot()
    explicit_manifest = body.get("manifest") if isinstance(body.get("manifest"), dict) else None

    if explicit_manifest is not None:
        manifest = explicit_manifest
    elif state.current_manifest is not None and (
        saved_manifest is None or snap.get("dest_dir") == resolved_dest
    ):
        manifest = state.current_manifest
    elif saved_manifest is not None:
        manifest = saved_manifest
    elif state.current_manifest is not None:
        manifest = state.current_manifest
    else:
        return JSONResponse(
            {
                "error": "لا توجد قائمة ملفات للفحص (لم يتم العثور على .sp_downloader_manifest.json في المجلد ولا توجد قائمة ممسوحة حالياً)."
            },
            status_code=400,
        )

    try:
        report = await asyncio.to_thread(verify_download, manifest, dest_dir)
        state.current_verification_report = report
        return JSONResponse(report)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_verify_export(request: Request) -> Response:
    if state.current_verification_report is None:
        return JSONResponse({"error": "لا يوجد تقرير تحقق حالي للتصدير. قم بإجراء التحقق أولاً."}, status_code=400)

    fmt = (request.query_params.get("format") or "csv").strip().lower()
    if fmt == "csv":
        content = export_verification_csv(state.current_verification_report)
        return Response(
            content=content.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="verification_report.csv"'},
        )
    if fmt == "json":
        content = export_verification_json(state.current_verification_report)
        return Response(
            content=content.encode("utf-8"),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="verification_report.json"'},
        )
    return JSONResponse({"error": "صيغة التصدير غير مدعومة (استخدم csv أو json)"}, status_code=400)


# ---------------------------------------------------------------------------
# Routes: Synchronization
# ---------------------------------------------------------------------------


async def api_sync_compare(request: Request) -> JSONResponse:
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    local_dir = str(body.get("local_dir") or "").strip()
    if not local_dir:
        return JSONResponse({"error": "يرجى تحديد المجلد المحلي للمقارنة والمزامنة"}, status_code=400)
    if os.path.isfile(win_long_path(local_dir)):
        return JSONResponse({"error": "المسار المحدد يشير إلى ملف وليس مجلداً"}, status_code=400)

    include_root_name = bool(body.get("include_root_name", False))
    root_name = body.get("root_name") or state.auth_manager.session.root_name
    compare_mtime = bool(body.get("compare_mtime", False))

    selected_items = body.get("selected_items")
    explicit_manifest = body.get("manifest") if isinstance(body.get("manifest"), dict) else None

    try:
        if explicit_manifest is not None:
            manifest = _apply_root_prefix(
                explicit_manifest,
                include_root_name,
                str(root_name) if root_name else None,
            )
            state.current_manifest = manifest
        elif isinstance(selected_items, list) and selected_items:
            manifest = await _scan_with_progress(
                selected_items=selected_items,
                include_root_name=include_root_name,
                root_name=str(root_name) if root_name else None,
            )
        elif state.current_manifest is not None:
            manifest = state.current_manifest
        else:
            return JSONResponse({"error": "يرجى تحديد مجلدات SharePoint المطلوب مزامنتها أولاً"}, status_code=400)

        sync_plan = await asyncio.to_thread(
            compare_sharepoint_with_local,
            manifest,
            local_dir,
            compare_mtime=compare_mtime,
        )
        state.current_sync_plan = sync_plan
        return JSONResponse(sync_plan)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_sync_download(request: Request) -> JSONResponse:
    if state.current_sync_plan is None:
        return JSONResponse({"error": "لا توجد نتائج مقارنة حالية. قم بتشغيل المقارنة أولاً."}, status_code=400)

    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    local_dir = str(body.get("local_dir") or state.current_sync_plan.get("local_dir") or "").strip()
    if not local_dir:
        return JSONResponse({"error": "يرجى تحديد المجلد المحلي للمزامنة"}, status_code=400)

    include_updated = bool(body.get("include_updated", False))
    try:
        concurrency = max(1, min(16, int(body.get("concurrency", 3))))
        max_retries = max(0, min(50, int(body.get("max_retries", 10))))
    except (TypeError, ValueError):
        return JSONResponse({"error": "قيم التوازي أو المحاولات غير صالحة"}, status_code=400)

    create_empty_folders = bool(body.get("create_empty_folders", True))
    skip_verified = bool(body.get("skip_verified", False))

    manifest_key = "all_changes_manifest" if include_updated else "new_only_manifest"
    sync_manifest = (
        state.current_sync_plan.get(manifest_key)
        or state.current_sync_plan.get("sync_manifest")
        or {"folders": [], "files": []}
    )

    try:
        snapshot = await state.download_manager.start_job(
            dest_dir=local_dir,
            manifest=sync_manifest,
            concurrency=concurrency,
            max_retries=max_retries,
            skip_verified=skip_verified,
            create_empty_folders=create_empty_folders,
            mode="sync",
        )
        return JSONResponse(
            {
                "started": True,
                "job_id": snapshot.get("job_id"),
                "include_updated": include_updated,
                "snapshot": snapshot,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_sync_report(request: Request) -> JSONResponse:
    if state.current_sync_plan is None:
        return JSONResponse({"error": "لا توجد خطة مزامنة حالية."}, status_code=400)

    snapshot = state.download_manager.get_snapshot()
    sync_snapshot = snapshot if snapshot.get("mode") == "sync" else None
    local_dir = (
        (sync_snapshot or {}).get("dest_dir")
        or state.current_sync_plan.get("local_dir")
        or ""
    )
    report = build_sync_completion_report(state.current_sync_plan, sync_snapshot, local_dir)
    return JSONResponse(report)


async def api_sync_export(request: Request) -> Response:
    if state.current_sync_plan is None:
        return JSONResponse({"error": "لا توجد بيانات مزامنة حالية للتصدير."}, status_code=400)

    fmt = (request.query_params.get("format") or "csv").strip().lower()
    scope = (request.query_params.get("scope") or "auto").strip().lower()

    snapshot = state.download_manager.get_snapshot()
    if scope == "plan" or snapshot.get("mode") != "sync" or not snapshot.get("job_id"):
        payload = state.current_sync_plan
        filename_stem = "sync_comparison"
    else:
        local_dir = snapshot.get("dest_dir") or state.current_sync_plan.get("local_dir") or ""
        payload = build_sync_completion_report(state.current_sync_plan, snapshot, local_dir)
        filename_stem = "sync_report"

    if fmt == "csv":
        content = export_sync_csv(payload)
        return Response(
            content=content.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename_stem}.csv"'},
        )
    if fmt == "json":
        content = export_sync_json(payload)
        return Response(
            content=content.encode("utf-8"),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename_stem}.json"'},
        )
    return JSONResponse({"error": "صيغة التصدير غير مدعومة (استخدم csv أو json)"}, status_code=400)


async def api_disk_compare(request: Request) -> Response:
    """Compare two local or network directories (Disk-to-Disk / Drive-to-Drive)."""
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    src = (body.get("source_dir") or "").strip()
    dst = (body.get("dest_dir") or "").strip()
    compare_hash = bool(body.get("compare_hash", False))

    if not src or not dst:
        return JSONResponse({"error": "يرجى تحديد مسار المجلد المصدر والمجلد الهدف"}, status_code=400)

    if not os.path.isdir(win_long_path(src)):
        return JSONResponse({"error": f"المجلد المصدر غير موجود: {src}"}, status_code=400)
    if not os.path.isdir(win_long_path(dst)):
        return JSONResponse({"error": f"المجلد الهدف غير موجود: {dst}"}, status_code=400)

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: compare_two_local_directories(src, dst, compare_hash=compare_hash),
    )
    state.current_disk_compare_result = result
    return JSONResponse(result)


async def api_disk_sync_start(request: Request) -> JSONResponse:
    """Start copying new/modified files from source to destination."""
    try:
        body = await _read_json_body(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    src = (body.get("source_dir") or "").strip()
    dst = (body.get("dest_dir") or "").strip()
    items = body.get("files") or []

    if not src or not dst:
        return JSONResponse({"error": "يرجى تحديد مسار المجلد المصدر والهدف"}, status_code=400)
    if not items:
        return JSONResponse({"error": "لا توجد ملفات محددة للمزامنة"}, status_code=400)

    try:
        state.disk_sync_manager.start_sync(src, dst, items)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse({"status": "started", "snapshot": state.disk_sync_manager.get_snapshot()})


async def api_disk_sync_pause(request: Request) -> JSONResponse:
    state.disk_sync_manager.pause()
    return JSONResponse({"status": "paused", "snapshot": state.disk_sync_manager.get_snapshot()})


async def api_disk_sync_resume(request: Request) -> JSONResponse:
    state.disk_sync_manager.resume()
    return JSONResponse({"status": "resumed", "snapshot": state.disk_sync_manager.get_snapshot()})


async def api_disk_sync_cancel(request: Request) -> JSONResponse:
    state.disk_sync_manager.cancel()
    return JSONResponse({"status": "cancelled", "snapshot": state.disk_sync_manager.get_snapshot()})


async def api_disk_sync_status(request: Request) -> JSONResponse:
    return JSONResponse(state.disk_sync_manager.get_snapshot())


# ---------------------------------------------------------------------------
# App Lifecycle & Entrypoint
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: Starlette):
    yield
    try:
        await state.download_manager.cancel()
    except Exception:
        pass
    try:
        if state.auth_manager.browser_open:
            await state.auth_manager.close_browser()
        elif state.auth_manager._playwright is not None:
            await state.auth_manager._playwright.stop()
            state.auth_manager._playwright = None
    except Exception:
        pass


routes = [
    Route("/", index_page, methods=["GET"]),
    Route("/favicon.ico", favicon_page, methods=["GET"]),
    Route("/favicon.svg", favicon_page, methods=["GET"]),
    Route("/api/health", api_health, methods=["GET"]),
    Route("/api/auth/open-browser", api_auth_open_browser, methods=["POST"]),
    Route("/api/auth/status", api_auth_status, methods=["GET"]),
    Route("/api/auth/close-browser", api_auth_close_browser, methods=["POST"]),
    Route("/api/auth/import-cookies", api_auth_import_cookies, methods=["POST"]),
    Route("/api/auth/clear", api_auth_clear, methods=["POST"]),
    Route("/api/system/pick-folder", api_system_pick_folder, methods=["POST"]),
    Route("/api/system/heartbeat", api_system_heartbeat, methods=["POST"]),
    Route("/api/system/client-leaving", api_system_client_leaving, methods=["POST"]),
    Route("/api/system/shutdown", api_system_shutdown, methods=["POST"]),
    Route("/api/disk/compare", api_disk_compare, methods=["POST"]),
    Route("/api/disk/sync/start", api_disk_sync_start, methods=["POST"]),
    Route("/api/disk/sync/pause", api_disk_sync_pause, methods=["POST"]),
    Route("/api/disk/sync/resume", api_disk_sync_resume, methods=["POST"]),
    Route("/api/disk/sync/cancel", api_disk_sync_cancel, methods=["POST"]),
    Route("/api/disk/sync/status", api_disk_sync_status, methods=["GET"]),
    Route("/api/sp/set-url", api_sp_set_url, methods=["POST"]),
    Route("/api/sp/root", api_sp_root, methods=["GET"]),
    Route("/api/sp/children", api_sp_children, methods=["POST"]),
    Route("/api/sp/scan", api_sp_scan, methods=["POST"]),
    Route("/api/sp/scan-status", api_sp_scan_status, methods=["GET"]),
    Route("/api/download/start", api_download_start, methods=["POST"]),
    Route("/api/download/pause", api_download_pause, methods=["POST"]),
    Route("/api/download/resume", api_download_resume, methods=["POST"]),
    Route("/api/download/cancel", api_download_cancel, methods=["POST"]),
    Route("/api/download/retry-failed", api_download_retry_failed, methods=["POST"]),
    Route("/api/download/status", api_download_status, methods=["GET"]),
    Route("/api/download/events", api_download_events, methods=["GET"]),
    Route("/api/verify", api_verify, methods=["POST"]),
    Route("/api/verify/export", api_verify_export, methods=["GET"]),
    Route("/api/sync/compare", api_sync_compare, methods=["POST"]),
    Route("/api/sync/download", api_sync_download, methods=["POST"]),
    Route("/api/sync/report", api_sync_report, methods=["GET"]),
    Route("/api/sync/export", api_sync_export, methods=["GET"]),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)


def find_free_port(start_port: int = 8765, host: str = "127.0.0.1", max_tries: int = 50) -> int:
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    _start_heartbeat_watchdog()

    host = "127.0.0.1"
    port = find_free_port(start_port=8765, host=host)
    url = f"http://{host}:{port}"
    print(f"SharePoint Direct Downloader running at {url}")

    if not os.environ.get("SP_NO_BROWSER"):
        timer = threading.Timer(0.9, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        loop="asyncio",
    )


if __name__ == "__main__":
    main()
