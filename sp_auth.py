"""Playwright-based SharePoint authentication, network context capture, and Cookie/cURL import."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from utils import extract_sharepoint_url_hints

APP_DIR = Path(__file__).resolve().parent
PROFILE_DIR = APP_DIR / "browser_profile"
SESSION_CACHE_FILE = APP_DIR / "session_cache.json"

LOGIN_HOSTS = (
    "login.microsoftonline.com",
    "login.live.com",
    "login.windows.net",
    "aadcdn.msauth.net",
    "aadcdn.msftauth.net",
)


@dataclass
class AuthSession:
    """Holds authenticated SharePoint session cookies, tokens, and resolved site metadata."""

    original_url: str = ""
    resolved_url: str = ""
    origin: str = ""
    web_url: str = ""
    web_server_relative: str = ""
    list_id: Optional[str] = None
    list_url: Optional[str] = None
    root_folder_server_relative: Optional[str] = None
    drive_id: Optional[str] = None
    root_item_id: Optional[str] = None
    root_name: Optional[str] = None
    cookies: list[dict[str, Any]] = field(default_factory=list)
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    )
    authorization_header: Optional[str] = None
    form_digest: Optional[str] = None
    detected_apis: list[str] = field(default_factory=list)
    authenticated: bool = False
    status_message: str = "غير متصل"
    updated_at: float = 0.0

    def to_httpx_cookies(self) -> httpx.Cookies:
        """Build an httpx.Cookies jar preserving domain & path."""
        jar = httpx.Cookies()
        for c in self.cookies:
            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue
            domain = c.get("domain") or ""
            path = c.get("path") or "/"
            if domain:
                jar.set(name, value, domain=domain, path=path)
            else:
                jar.set(name, value, path=path)
        return jar

    def cookie_header_for_url(self, url: str) -> str:
        """Return Cookie header string matching a target URL's host and path."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"
        pairs: list[str] = []
        seen: set[str] = set()
        for c in self.cookies:
            c_domain = (c.get("domain") or "").lstrip(".").lower()
            c_path = c.get("path") or "/"
            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue
            domain_match = (not c_domain) or (host == c_domain) or host.endswith("." + c_domain)
            path_match = path.startswith(c_path)
            if domain_match and path_match and name not in seen:
                pairs.append(f"{name}={value}")
                seen.add(name)
        return "; ".join(pairs)

    def has_sharepoint_auth_cookie(self) -> bool:
        """Check whether typical SharePoint session cookies exist."""
        names = {c.get("name", "") for c in self.cookies}
        return any(
            k in names
            for k in ("FedAuth", "rtFa", "SPOIDCRL", "SIMI", "sp_auth", "OIDCAuth")
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Safe summary for the web UI (omits raw secret cookie values)."""
        return {
            "original_url": self.original_url,
            "resolved_url": self.resolved_url,
            "origin": self.origin,
            "web_url": self.web_url,
            "list_id": self.list_id,
            "list_url": self.list_url,
            "root_folder_server_relative": self.root_folder_server_relative,
            "drive_id": self.drive_id,
            "root_item_id": self.root_item_id,
            "root_name": self.root_name,
            "cookie_count": len(self.cookies),
            "has_auth_cookie": self.has_sharepoint_auth_cookie(),
            "has_bearer": bool(self.authorization_header),
            "detected_apis": self.detected_apis,
            "authenticated": self.authenticated,
            "status_message": self.status_message,
            "updated_at": self.updated_at,
        }


def _is_login_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if any(h in host for h in LOGIN_HOSTS):
        return True
    if "/_layouts/15/authenticate.aspx" in path or "/adfs/ls" in path:
        return True
    return False


def _parse_sp_item_url(sp_item_url: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (driveId, itemId) from a SharePoint v2.0 driveItem URL."""
    m = re.search(r"/_api/v2\.0/drives/([^/]+)/items/([^/?]+)", sp_item_url, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    return None, None


class BrowserAuthManager:
    """Manages persistent Playwright browser login, network sniffing, and session refresh."""

    def __init__(self) -> None:
        self.session = AuthSession()
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._watcher_task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self.browser_open = False
        self._load_cached_session()

    def _load_cached_session(self) -> None:
        if not SESSION_CACHE_FILE.exists():
            return
        try:
            data = json.loads(SESSION_CACHE_FILE.read_text(encoding="utf-8"))
            self.session = AuthSession(**data)
            if self.session.cookies:
                self.session.status_message = "توجد جلسة محفوظة من تشغيل سابق"
        except Exception:
            pass

    def _save_cached_session(self) -> None:
        try:
            SESSION_CACHE_FILE.write_text(
                json.dumps(asdict(self.session), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    async def _launch_persistent(self, headless: bool) -> BrowserContext:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        last_err: Optional[Exception] = None
        # Prefer native Edge or Chrome on Windows for seamless Microsoft login, fallback to bundled Chromium
        for channel in ("msedge", "chrome", None):
            try:
                kwargs: dict[str, Any] = {
                    "user_data_dir": str(PROFILE_DIR),
                    "headless": headless,
                    "accept_downloads": False,
                    "viewport": {"width": 1240, "height": 840},
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                if channel:
                    kwargs["channel"] = channel
                ctx = await self._playwright.chromium.launch_persistent_context(**kwargs)
                return ctx
            except Exception as exc:
                last_err = exc
                continue
        raise RuntimeError(f"تعذر تشغيل المتصفح: {last_err}")

    def _attach_listeners(self, page: Page) -> None:
        def on_request(req) -> None:
            url = req.url
            if "/_api/" not in url:
                return
            headers = req.headers
            auth = headers.get("authorization") or headers.get("Authorization")
            if auth and auth.lower().startswith("bearer "):
                self.session.authorization_header = auth
            digest = headers.get("x-requestdigest") or headers.get("X-RequestDigest")
            if digest:
                self.session.form_digest = digest

        async def on_response(resp) -> None:
            url = resp.url
            if "/_api/" not in url or resp.status >= 400:
                return
            if "/_api/v2.0/drives/" in url:
                if "v2.0_drives" not in self.session.detected_apis:
                    self.session.detected_apis.append("v2.0_drives")
                lower_url = url.lower()
                if "/thumbnails" not in lower_url and "/permissions" not in lower_url:
                    d_id, i_id = _parse_sp_item_url(url)
                    # Only use root-level folder drive/item if we don't have one yet
                    if d_id and not self.session.drive_id:
                        self.session.drive_id = d_id
                        if i_id and i_id.lower() != "root":
                            self.session.root_item_id = i_id
            elif "/_api/v2.0/shares/" in url:
                if "v2.0_shares" not in self.session.detected_apis:
                    self.session.detected_apis.append("v2.0_shares")
                try:
                    if "/driveitem" in url.lower() and "application/json" in (
                        resp.headers.get("content-type") or ""
                    ):
                        payload = await resp.json()
                        if isinstance(payload, dict) and payload.get("id"):
                            parent_ref = payload.get("parentReference") or {}
                            if parent_ref.get("driveId"):
                                self.session.drive_id = parent_ref["driveId"]
                            self.session.root_item_id = payload["id"]
                            if payload.get("name"):
                                self.session.root_name = payload["name"]
                except Exception:
                    pass
            elif "RenderListDataAsStream" in url:
                if "RenderListDataAsStream" not in self.session.detected_apis:
                    self.session.detected_apis.append("RenderListDataAsStream")

        page.on("request", on_request)
        page.on("response", lambda resp: asyncio.create_task(on_response(resp)))

    async def _sync_from_page(self) -> None:
        if not self._context or not self._page:
            return
        try:
            if self._page.is_closed():
                self.browser_open = False
                return
            current_url = self._page.url or ""
            if current_url and not current_url.startswith("about:"):
                self.session.resolved_url = current_url
                hints = extract_sharepoint_url_hints(current_url)
                if hints["origin"]:
                    self.session.origin = hints["origin"]
                if hints["web_url"] and not self.session.web_url:
                    self.session.web_url = hints["web_url"]
                if hints["folder_id"]:
                    self.session.root_folder_server_relative = hints["folder_id"]

            cookies = await self._context.cookies()
            if cookies:
                self.session.cookies = cookies
                self.session.updated_at = time.time()

            # Read SharePoint JS context variables when on a non-login SharePoint page
            if current_url.startswith("http") and not _is_login_url(current_url):
                ctx_info = await self._page.evaluate(
                    """() => {
                        const out = { ua: navigator.userAgent };
                        if (window._spPageContextInfo) {
                            const p = window._spPageContextInfo;
                            out.webAbsoluteUrl = p.webAbsoluteUrl || null;
                            out.webServerRelativeUrl = p.webServerRelativeUrl || null;
                            out.listId = p.listId || null;
                            out.listUrl = p.listUrl || null;
                            out.listTitle = p.listTitle || null;
                            out.formDigestValue = p.formDigestValue || null;
                        }
                        if (window.g_listData && window.g_listData.ListData) {
                            const ld = window.g_listData.ListData;
                            out.currentFolderSpItemUrl = ld.CurrentFolderSpItemUrl || null;
                            out.rootFolder = ld.RootFolder || (window.g_listData.ListSchema && window.g_listData.ListSchema.RootFolder) || null;
                        }
                        const digestEl = document.getElementById('__REQUESTDIGEST');
                        if (digestEl && digestEl.value) {
                            out.domDigest = digestEl.value;
                        }
                        return out;
                    }"""
                )
                if isinstance(ctx_info, dict):
                    if ctx_info.get("ua"):
                        self.session.user_agent = ctx_info["ua"]
                    if ctx_info.get("webAbsoluteUrl"):
                        self.session.web_url = ctx_info["webAbsoluteUrl"].rstrip("/")
                        parsed_w = urlparse(self.session.web_url)
                        self.session.origin = f"{parsed_w.scheme}://{parsed_w.netloc}"
                    if ctx_info.get("webServerRelativeUrl"):
                        self.session.web_server_relative = ctx_info["webServerRelativeUrl"]
                    if ctx_info.get("listId"):
                        self.session.list_id = ctx_info["listId"].strip("{}")
                    if ctx_info.get("listUrl"):
                        self.session.list_url = ctx_info["listUrl"]
                    digest = ctx_info.get("formDigestValue") or ctx_info.get("domDigest")
                    if digest and digest != "InvalidFormDigest":
                        self.session.form_digest = digest
                    if ctx_info.get("currentFolderSpItemUrl"):
                        d_id, i_id = _parse_sp_item_url(ctx_info["currentFolderSpItemUrl"])
                        if d_id:
                            self.session.drive_id = d_id
                        if i_id:
                            self.session.root_item_id = i_id
                    if ctx_info.get("rootFolder") and not self.session.root_folder_server_relative:
                        self.session.root_folder_server_relative = ctx_info["rootFolder"]

            # Determine authentication state
            is_on_sp = (
                bool(current_url)
                and not _is_login_url(current_url)
                and not current_url.startswith("about:")
                and ("guestaccess.aspx" not in current_url.lower() or self.session.has_sharepoint_auth_cookie())
            )
            has_context = bool(
                self.session.has_sharepoint_auth_cookie()
                or self.session.drive_id
                or self.session.list_id
                or self.session.root_folder_server_relative
                or self.session.detected_apis
            )
            if is_on_sp and has_context:
                self.session.authenticated = True
                self.session.status_message = "تم تسجيل الدخول والتقاط الجلسة والكوكيز بنجاح"
                self._save_cached_session()
            elif _is_login_url(current_url):
                self.session.authenticated = False
                self.session.status_message = "في انتظار إكمال تسجيل الدخول / كود التحقق في نافذة المتصفح..."
            elif current_url:
                self.session.status_message = "جاري تحميل صفحة SharePoint..."
        except Exception:
            # Page may be navigating or closed by the user
            pass

    async def _watch_loop(self) -> None:
        while self.browser_open and self._context and self._page:
            await self._sync_from_page()
            if self._page and self._page.is_closed():
                self.browser_open = False
                break
            await asyncio.sleep(1.0)

    async def start_interactive_login(self, sharepoint_url: str) -> AuthSession:
        """Open a real persistent Edge/Chrome browser window to the SharePoint URL."""
        async with self._lock:
            await self._close_context_only()

            hints = extract_sharepoint_url_hints(sharepoint_url)
            self.session = AuthSession(
                original_url=sharepoint_url.strip(),
                origin=hints["origin"] or "",
                web_url=hints["web_url"] or "",
                web_server_relative=hints["web_path"] or "",
                root_folder_server_relative=hints["folder_id"],
                status_message="جاري فتح المتصفح...",
            )

            ctx = await self._launch_persistent(headless=False)
            self._context = ctx
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            self._page = page
            self.browser_open = True

            def _on_ctx_close() -> None:
                self.browser_open = False

            ctx.on("close", lambda *_: _on_ctx_close())
            page.on("close", lambda *_: _on_ctx_close())
            self._attach_listeners(page)

            try:
                await page.goto(sharepoint_url.strip(), wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                # Navigation might still be in progress (SSO redirects)
                self.session.status_message = f"تم فتح الصفحة ({exc}); أكمل الدخول في النافذة..."

            await self._sync_from_page()
            self._watcher_task = asyncio.create_task(self._watch_loop())
            return self.session

    async def sync_and_get_session(self) -> AuthSession:
        """Force immediate sync if browser is open and return current session."""
        if self.browser_open:
            await self._sync_from_page()
        return self.session

    async def close_browser(self) -> AuthSession:
        """Sync session then close the visible Playwright browser window."""
        async with self._lock:
            if self.browser_open:
                await self._sync_from_page()
            self._save_cached_session()
            await self._close_context_only()
            return self.session

    async def _close_context_only(self) -> None:
        self.browser_open = False
        if self._watcher_task and not self._watcher_task.done():
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except asyncio.CancelledError:
                pass
        self._watcher_task = None
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        self._context = None
        self._page = None

    async def refresh_session_headless(self) -> bool:
        """
        Attempt to silently refresh expired cookies/tokens using the saved persistent profile.
        Returns True if still authenticated without manual interaction.
        """
        target_url = self.session.resolved_url or self.session.original_url
        if not target_url or not PROFILE_DIR.exists():
            return False
        async with self._lock:
            # If interactive browser is already open, just sync from it
            if self.browser_open and self._page and not self._page.is_closed():
                try:
                    await self._page.reload(wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2.5)
                    await self._sync_from_page()
                    return self.session.authenticated
                except Exception:
                    return False

            ctx: Optional[BrowserContext] = None
            try:
                ctx = await self._launch_persistent(headless=True)
                self._context = ctx
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                self._page = page
                self._attach_listeners(page)
                await page.goto(target_url, wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(3.0)
                await self._sync_from_page()
                return self.session.authenticated and not _is_login_url(page.url)
            except Exception:
                return False
            finally:
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                self._context = None
                self._page = None

    async def browser_fetch_json(self, url: str, method: str = "GET", body: Any = None, headers: Optional[dict[str, str]] = None) -> tuple[int, Any]:
        """
        Fallback helper: execute an API call directly inside the authenticated Playwright page
        if direct HTTP requests are blocked by conditional access / TLS binding.
        """
        if not self._page or self._page.is_closed():
            raise RuntimeError("نافذة المتصفح مغلقة")
        return await self._page.evaluate(
            """async ({ url, method, body, headers }) => {
                const opts = {
                    method,
                    credentials: 'include',
                    headers: headers || { 'Accept': 'application/json' },
                };
                if (body !== null && body !== undefined) {
                    opts.body = typeof body === 'string' ? body : JSON.stringify(body);
                }
                const resp = await fetch(url, opts);
                const text = await resp.text();
                try {
                    return [resp.status, JSON.parse(text)];
                } catch {
                    return [resp.status, text];
                }
            }""",
            {"url": url, "method": method, "body": body, "headers": headers},
        )

    def import_from_curl_or_cookies(self, raw_text: str, sharepoint_url: str) -> AuthSession:
        """
        Parse raw Cookie header (`FedAuth=...; rtFa=...`) or a DevTools `Copy as cURL` snippet.
        """
        text = raw_text.strip()
        parsed_url = sharepoint_url.strip()
        cookie_str = ""
        user_agent = self.session.user_agent
        auth_header: Optional[str] = None
        form_digest: Optional[str] = None

        if text.lower().startswith("curl "):
            # Handle line continuations (`\` in bash, `^` in Windows cmd) and cmd caret escapes
            normalized = re.sub(r"\\\r?\n", " ", text)
            normalized = re.sub(r"\^\r?\n", " ", normalized)
            normalized = re.sub(r"\^([^\r\n])", r"\1", normalized)
            try:
                tokens = shlex.split(normalized, posix=True)
            except ValueError:
                tokens = normalized.split()

            i = 0
            while i < len(tokens):
                tok = tokens[i]
                if tok in ("-H", "--header") and i + 1 < len(tokens):
                    hdr = tokens[i + 1]
                    k, _, v = hdr.partition(":")
                    kl = k.strip().lower()
                    val = v.strip()
                    if kl == "cookie":
                        cookie_str = val
                    elif kl == "user-agent":
                        user_agent = val
                    elif kl == "authorization":
                        auth_header = val
                    elif kl == "x-requestdigest":
                        form_digest = val
                    i += 2
                    continue
                if tok in ("-b", "--cookie") and i + 1 < len(tokens):
                    cookie_str = tokens[i + 1].strip()
                    i += 2
                    continue
                if tok.startswith("http://") or tok.startswith("https://"):
                    if not parsed_url:
                        parsed_url = tok
                i += 1
        else:
            # Plain cookie header (`Cookie: a=1; b=2` or `a=1; b=2`)
            if text.lower().startswith("cookie:"):
                cookie_str = text.split(":", 1)[1].strip()
            else:
                cookie_str = text

        if not parsed_url:
            raise ValueError("يرجى إدخال رابط SharePoint أولاً")
        if not cookie_str and not auth_header:
            raise ValueError("لم يتم العثور على Cookie أو Authorization في النص الملصق")

        hints = extract_sharepoint_url_hints(parsed_url)
        host = urlparse(parsed_url).hostname or ""

        cookies: list[dict[str, Any]] = []
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            name = name.strip()
            if not name:
                continue
            cookies.append(
                {
                    "name": name,
                    "value": value.strip(),
                    "domain": host,
                    "path": "/",
                }
            )

        self.session = AuthSession(
            original_url=parsed_url,
            resolved_url=parsed_url,
            origin=hints["origin"] or "",
            web_url=hints["web_url"] or "",
            web_server_relative=hints["web_path"] or "",
            root_folder_server_relative=hints["folder_id"],
            cookies=cookies,
            user_agent=user_agent,
            authorization_header=auth_header,
            form_digest=form_digest,
            authenticated=True,
            status_message="تم استيراد الكوكيز بنجاح",
            updated_at=time.time(),
        )
        self._save_cached_session()
        return self.session

    async def clear_session(self) -> None:
        """Close browser and wipe saved profile & cached cookies."""
        async with self._lock:
            await self._close_context_only()
            self.session = AuthSession(status_message="تم مسح الجلسة السابقة")
            if SESSION_CACHE_FILE.exists():
                SESSION_CACHE_FILE.unlink(missing_ok=True)
            if PROFILE_DIR.exists():
                shutil.rmtree(PROFILE_DIR, ignore_errors=True)
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
