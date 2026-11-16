"""Universal SharePoint Explorer & Recursive Scanner supporting v2.0 (OneDrive/Graph),
RenderListDataAsStream, and REST v1 APIs with automatic fallback and throttling protection.
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, unquote, urlparse

import httpx

from sp_auth import AuthSession, BrowserAuthManager
from utils import (
    encode_sharing_url,
    encode_sp_path_param,
    extract_sharepoint_url_hints,
)


@dataclass
class SPItem:
    """Represents a SharePoint folder or file."""

    id: str
    name: str
    is_folder: bool
    size: int = 0
    relative_path: str = ""
    server_relative_path: Optional[str] = None
    drive_id: Optional[str] = None
    item_id: Optional[str] = None
    download_url: Optional[str] = None
    fallback_download_urls: list[str] = field(default_factory=list)
    last_modified: str = ""
    child_count: Optional[int] = None
    backend: str = "v2"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_rows(data: Any) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Handle both odata=nometadata and odata=verbose SharePoint JSON list payloads."""
    if not isinstance(data, dict):
        return [], None
    if "value" in data and isinstance(data["value"], list):
        next_link = data.get("@odata.nextLink") or data.get("odata.nextLink")
        return data["value"], next_link
    if "d" in data and isinstance(data["d"], dict):
        d = data["d"]
        if "results" in d and isinstance(d["results"], list):
            return d["results"], d.get("__next")
        return [d], None
    return [], None


def _build_rest_download_urls(web_url: str, server_relative_path: str) -> list[str]:
    """Generate direct file download URLs (no zipping) for SharePoint REST/layouts."""
    if not web_url or not server_relative_path:
        return []
    base = web_url.rstrip("/")
    encoded_p = encode_sp_path_param(server_relative_path)
    encoded_src = quote(server_relative_path, safe="/")
    return [
        f"{base}/_api/web/GetFileByServerRelativePath(decodedurl=@p)/$value?@p='{encoded_p}'",
        f"{base}/_layouts/15/download.aspx?SourceUrl={encoded_src}",
        f"{base}/_api/web/GetFileByServerRelativeUrl('{encoded_p}')/$value",
    ]


class SharePointClient:
    """Fetches folder trees, scans nested contents recursively, and refreshes download URLs."""

    def __init__(self, auth_manager: BrowserAuthManager) -> None:
        self.auth = auth_manager

    @property
    def session(self) -> AuthSession:
        return self.auth.session

    def _build_headers(self, url: str, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        s = self.session
        headers: dict[str, str] = {
            "Accept": "application/json;odata=nometadata",
            "User-Agent": s.user_agent,
        }
        if s.origin:
            headers["Origin"] = s.origin
            headers["Referer"] = s.resolved_url or s.original_url or s.origin
        if s.authorization_header and "/_api/v2.0/" in url:
            headers["Authorization"] = s.authorization_header
        cookie_hdr = s.cookie_header_for_url(url)
        if cookie_hdr:
            headers["Cookie"] = cookie_hdr
        if extra:
            headers.update(extra)
        return headers

    def _new_httpx_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=25.0, read=60.0, write=30.0, pool=20.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        json_body: Any = None,
        extra_headers: Optional[dict[str, str]] = None,
        retries: int = 4,
        allow_browser_fallback: bool = True,
    ) -> tuple[int, Any]:
        """Execute SharePoint API JSON call with 429 Retry-After, 401 refresh, and browser fallback."""
        refreshed_once = False
        for attempt in range(retries + 1):
            headers = self._build_headers(url, extra_headers)
            if json_body is not None and "Content-Type" not in headers:
                headers["Content-Type"] = "application/json;odata=verbose"
            try:
                resp = await client.request(method, url, headers=headers, json=json_body)
            except (httpx.RequestError, httpx.StreamError):
                if attempt < retries:
                    await asyncio.sleep(min(15.0, 1.5 * (2 ** attempt) + random.uniform(0, 0.5)))
                    continue
                raise

            # Check if redirected to Microsoft login page
            final_host = (resp.url.host or "").lower()
            if any(h in final_host for h in ("login.microsoftonline.com", "login.live.com", "login.windows.net")):
                if not refreshed_once:
                    refreshed_once = True
                    if await self.auth.refresh_session_headless():
                        continue
                return 401, {"error": "انتهت صلاحية الجلسة وتم التوجيه لصفحة تسجيل الدخول"}

            if resp.status_code in (429, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else (2.0 * (2 ** attempt))
                await asyncio.sleep(min(60.0, delay + random.uniform(0.2, 1.0)))
                continue

            if resp.status_code in (401, 403):
                # Refresh digest on 403 for POST calls, or refresh headless session once
                if method.upper() == "POST" and resp.status_code == 403 and not refreshed_once:
                    refreshed_once = True
                    digest = await self._fetch_form_digest(client)
                    if digest:
                        extra_headers = dict(extra_headers or {})
                        extra_headers["X-RequestDigest"] = digest
                        continue
                if not refreshed_once and await self.auth.refresh_session_headless():
                    refreshed_once = True
                    continue
                if allow_browser_fallback and self.auth.browser_open:
                    try:
                        b_headers = {"Accept": headers["Accept"]}
                        if extra_headers and "X-RequestDigest" in extra_headers:
                            b_headers["X-RequestDigest"] = extra_headers["X-RequestDigest"]
                        if json_body is not None:
                            b_headers["Content-Type"] = headers["Content-Type"]
                        return await self.auth.browser_fetch_json(url, method, json_body, b_headers)
                    except Exception:
                        pass

            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, resp.text

        return 500, {"error": "فشلت المحاولات المتكررة للاتصال بـ SharePoint"}

    async def _fetch_form_digest(self, client: httpx.AsyncClient) -> Optional[str]:
        web_url = (self.session.web_url or self.session.origin).rstrip("/")
        if not web_url:
            return None
        url = f"{web_url}/_api/contextinfo"
        headers = self._build_headers(url, {"Accept": "application/json;odata=nometadata"})
        try:
            resp = await client.post(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                digest = (
                    data.get("FormDigestValue")
                    or ((data.get("d") or {}).get("GetContextWebInformation") or {}).get("FormDigestValue")
                )
                if digest:
                    self.session.form_digest = digest
                    return digest
        except Exception:
            pass
        return None

    def _api_base(self) -> str:
        return (self.session.web_url or self.session.origin).rstrip("/")

    async def resolve_root(self) -> SPItem:
        """
        Resolve the root folder represented by the current SharePoint session/URL,
        probing v2.0 shares/drives, REST v1, and RenderListDataAsStream.
        """
        s = self.session
        if not s.origin and (s.resolved_url or s.original_url):
            hints = extract_sharepoint_url_hints(s.resolved_url or s.original_url)
            s.origin = hints["origin"] or ""
            if not s.web_url and hints["web_url"]:
                s.web_url = hints["web_url"]
            if not s.root_folder_server_relative and hints["folder_id"]:
                s.root_folder_server_relative = hints["folder_id"]

        if not s.origin:
            raise ValueError("رابط SharePoint غير صالح أو لم يتم تحديد الموقع بعد")

        async with self._new_httpx_client() as client:
            # 1) Try already captured (drive_id, root_item_id) from network traffic or g_listData
            if s.drive_id and s.root_item_id:
                for base in dict.fromkeys(b for b in (s.web_url, s.origin) if b):
                    url = f"{base.rstrip('/')}/_api/v2.0/drives/{s.drive_id}/items/{s.root_item_id}"
                    code, data = await self._request_json(client, "GET", url)
                    if code == 200 and isinstance(data, dict) and data.get("id"):
                        return self._v2_dict_to_item(data, rel_path="", drive_id=s.drive_id)

            # 2) Try v2.0 shares endpoint on original_url (works great for /:f:/s/... & guest links)
            candidates = []
            for raw_url in (s.original_url, s.resolved_url):
                if not raw_url:
                    continue
                candidates.append(raw_url)
                no_q = raw_url.split("?")[0]
                if no_q != raw_url:
                    candidates.append(no_q)
            for share_url in dict.fromkeys(candidates):
                share_id = encode_sharing_url(share_url)
                for base in dict.fromkeys(b for b in (s.origin, s.web_url) if b):
                    url = f"{base.rstrip('/')}/_api/v2.0/shares/{share_id}/driveItem"
                    code, data = await self._request_json(client, "GET", url)
                    if code == 200 and isinstance(data, dict) and data.get("id"):
                        parent_ref = data.get("parentReference") or {}
                        d_id = parent_ref.get("driveId") or s.drive_id
                        if d_id:
                            s.drive_id = d_id
                        s.root_item_id = data["id"]
                        s.root_name = data.get("name") or s.root_name
                        return self._v2_dict_to_item(data, rel_path="", drive_id=d_id)

            # 3) Resolve folder server-relative path for REST v1 / RenderListDataAsStream
            folder_path = s.root_folder_server_relative or self._infer_folder_path_from_urls()
            web_url = await self._ensure_web_url(client, folder_path)
            if folder_path and web_url:
                s.root_folder_server_relative = folder_path
                # Try REST v1 GetFolderByServerRelativePath
                enc = encode_sp_path_param(folder_path)
                url = f"{web_url}/_api/web/GetFolderByServerRelativePath(decodedurl=@p)?@p='{enc}'"
                code, data = await self._request_json(client, "GET", url)
                if code == 200 and isinstance(data, dict):
                    folder_obj = data.get("d") if isinstance(data.get("d"), dict) else data
                    name = folder_obj.get("Name") or folder_path.rstrip("/").split("/")[-1] or "SharePoint Root"
                    return SPItem(
                        id=folder_obj.get("UniqueId") or folder_path,
                        name=name,
                        is_folder=True,
                        size=0,
                        relative_path="",
                        server_relative_path=folder_obj.get("ServerRelativeUrl") or folder_path,
                        child_count=folder_obj.get("ItemCount"),
                        backend="rest_v1",
                    )

                # Fallback: RenderListDataAsStream root probe
                if s.list_id or s.list_url or folder_path:
                    probe_name = folder_path.rstrip("/").split("/")[-1] or "SharePoint Root"
                    probe_item = SPItem(
                        id=folder_path,
                        name=probe_name,
                        is_folder=True,
                        size=0,
                        relative_path="",
                        server_relative_path=folder_path,
                        backend="render_list",
                    )
                    # Verify we can actually list it
                    try:
                        await self._list_render_stream(client, probe_item)
                        return probe_item
                    except Exception:
                        pass

            raise PermissionError(
                "تعذر قراءة محتويات رابط SharePoint. تأكد من إتمام تسجيل الدخول في المتصفح أو من صلاحية الرابط."
            )

    def _infer_folder_path_from_urls(self) -> Optional[str]:
        for u in (self.session.resolved_url, self.session.original_url):
            if not u:
                continue
            hints = extract_sharepoint_url_hints(u)
            if hints["folder_id"]:
                return hints["folder_id"]
            parsed = urlparse(u)
            p = unquote(parsed.path)
            # e.g. /sites/Projects/Shared Documents/Forms/AllItems.aspx -> /sites/Projects/Shared Documents
            m = re.match(r"^(.+?)/Forms/[^/]+\.aspx$", p, re.IGNORECASE)
            if m:
                return m.group(1)
            # Direct folder path under /sites/<name>/... (excluding _layouts and sharing /:f:/)
            if not parsed.path.startswith("/:") and "/_layouts/" not in parsed.path.lower():
                if parsed.path and parsed.path != "/":
                    return p.rstrip("/")
        return None

    async def _ensure_web_url(self, client: httpx.AsyncClient, folder_path: Optional[str]) -> str:
        if self.session.web_url:
            return self.session.web_url.rstrip("/")
        candidates = []
        if folder_path:
            parts = [p for p in folder_path.split("/") if p]
            if len(parts) >= 2 and parts[0].lower() in ("sites", "teams", "personal"):
                candidates.append(f"{self.session.origin}/{parts[0]}/{parts[1]}")
            if len(parts) >= 3 and parts[0].lower() in ("sites", "teams"):
                candidates.append(f"{self.session.origin}/{parts[0]}/{parts[1]}/{parts[2]}")
        candidates.append(self.session.origin)
        for cand in candidates:
            code, data = await self._request_json(
                client, "GET", f"{cand.rstrip('/')}/_api/web?$select=Url,ServerRelativeUrl"
            )
            if code == 200 and isinstance(data, dict):
                w = data.get("Url") or (data.get("d") or {}).get("Url")
                if w:
                    self.session.web_url = w.rstrip("/")
                    return self.session.web_url
        return (self.session.web_url or self.session.origin).rstrip("/")

    def _v2_dict_to_item(
        self,
        item: dict[str, Any],
        rel_path: str,
        drive_id: Optional[str] = None,
    ) -> SPItem:
        parent_ref = item.get("parentReference") or {}
        d_id = drive_id or parent_ref.get("driveId") or self.session.drive_id
        i_id = item.get("id", "")
        is_folder = "folder" in item
        name = item.get("name") or "unnamed"
        size = 0 if is_folder else int(item.get("size", -1) if item.get("size") is not None else -1)
        dl_url = item.get("@content.downloadUrl") or item.get("@microsoft.graph.downloadUrl")
        fallbacks: list[str] = []
        base = self._api_base()
        if d_id and i_id and not is_folder:
            fallbacks.append(f"{base}/_api/v2.0/drives/{d_id}/items/{i_id}/content")
        srv_path: Optional[str] = None
        if parent_ref.get("path") and "/root:" in parent_ref["path"]:
            # Best-effort server path hint if needed
            pass
        return SPItem(
            id=f"{d_id}:{i_id}" if d_id else i_id,
            name=name,
            is_folder=is_folder,
            size=size,
            relative_path=rel_path,
            server_relative_path=srv_path,
            drive_id=d_id,
            item_id=i_id,
            download_url=dl_url or (fallbacks[0] if fallbacks else None),
            fallback_download_urls=fallbacks,
            last_modified=item.get("lastModifiedDateTime") or "",
            child_count=(item.get("folder") or {}).get("childCount") if is_folder else None,
            backend="v2",
        )

    async def list_children(self, folder: SPItem) -> list[SPItem]:
        """List immediate children (folders first, then files) of a SharePoint folder."""
        async with self._new_httpx_client() as client:
            return await self._list_children_with_client(client, folder)

    async def _list_children_with_client(
        self,
        client: httpx.AsyncClient,
        folder: SPItem,
    ) -> list[SPItem]:
        errors: list[str] = []
        # Order backends based on what succeeded for parent, with automatic fallback
        order = [folder.backend] + [b for b in ("v2", "rest_v1", "render_list") if b != folder.backend]
        for backend in order:
            try:
                if backend == "v2" and folder.drive_id and folder.item_id:
                    items = await self._list_v2(client, folder)
                    return self._sort_items(items)
                if backend == "rest_v1" and folder.server_relative_path:
                    items = await self._list_rest_v1(client, folder)
                    return self._sort_items(items)
                if backend == "render_list" and folder.server_relative_path:
                    items = await self._list_render_stream(client, folder)
                    return self._sort_items(items)
            except Exception as exc:
                errors.append(f"{backend}: {exc}")
                continue
        raise RuntimeError("تعذر قراءة محتويات المجلد: " + " | ".join(errors))

    @staticmethod
    def _sort_items(items: list[SPItem]) -> list[SPItem]:
        return sorted(items, key=lambda x: (not x.is_folder, x.name.lower()))

    async def _list_v2(self, client: httpx.AsyncClient, folder: SPItem) -> list[SPItem]:
        base = self._api_base()
        url: Optional[str] = (
            f"{base}/_api/v2.0/drives/{folder.drive_id}/items/{folder.item_id}/children?$top=500"
        )
        results: list[SPItem] = []
        while url:
            code, data = await self._request_json(client, "GET", url)
            if code != 200 or not isinstance(data, dict):
                raise RuntimeError(f"v2 API status {code}")
            rows, next_url = _extract_rows(data)
            for row in rows:
                # Skip virtual OneNote notebooks/sections that aren't normal files or folders
                if "folder" not in row and "file" not in row:
                    continue
                name = row.get("name") or ""
                if not name:
                    continue
                child_rel = f"{folder.relative_path}/{name}".lstrip("/")
                results.append(self._v2_dict_to_item(row, rel_path=child_rel, drive_id=folder.drive_id))
            url = next_url
        return results

    async def _list_rest_v1(self, client: httpx.AsyncClient, folder: SPItem) -> list[SPItem]:
        web_url = (self.session.web_url or self.session.origin).rstrip("/")
        srv_path = folder.server_relative_path or ""
        enc = encode_sp_path_param(srv_path)
        results: list[SPItem] = []

        # 1) Folders
        f_url: Optional[str] = (
            f"{web_url}/_api/web/GetFolderByServerRelativePath(decodedurl=@p)/Folders?$top=5000&@p='{enc}'"
        )
        while f_url:
            code, data = await self._request_json(client, "GET", f_url)
            if code != 200:
                raise RuntimeError(f"REST Folders status {code}")
            rows, next_url = _extract_rows(data)
            for row in rows:
                name = row.get("Name") or ""
                child_srv = row.get("ServerRelativeUrl") or f"{srv_path.rstrip('/')}/{name}"
                # Skip internal SharePoint Forms directory at document library root
                if name == "Forms" and (
                    (self.session.list_url and srv_path.rstrip("/").lower() == self.session.list_url.rstrip("/").lower())
                    or child_srv.lower().endswith("/shared documents/forms")
                ):
                    continue
                child_rel = f"{folder.relative_path}/{name}".lstrip("/")
                results.append(
                    SPItem(
                        id=row.get("UniqueId") or child_srv,
                        name=name,
                        is_folder=True,
                        size=0,
                        relative_path=child_rel,
                        server_relative_path=child_srv,
                        last_modified=row.get("TimeLastModified") or "",
                        child_count=row.get("ItemCount"),
                        backend="rest_v1",
                    )
                )
            f_url = next_url

        # 2) Files
        file_url: Optional[str] = (
            f"{web_url}/_api/web/GetFolderByServerRelativePath(decodedurl=@p)/Files?$top=5000&@p='{enc}'"
        )
        while file_url:
            code, data = await self._request_json(client, "GET", file_url)
            if code != 200:
                raise RuntimeError(f"REST Files status {code}")
            rows, next_url = _extract_rows(data)
            for row in rows:
                name = row.get("Name") or ""
                child_srv = row.get("ServerRelativeUrl") or f"{srv_path.rstrip('/')}/{name}"
                raw_len = row.get("Length")
                size = int(raw_len) if raw_len is not None and str(raw_len).isdigit() else -1
                dl_urls = _build_rest_download_urls(web_url, child_srv)
                child_rel = f"{folder.relative_path}/{name}".lstrip("/")
                results.append(
                    SPItem(
                        id=row.get("UniqueId") or child_srv,
                        name=name,
                        is_folder=False,
                        size=size,
                        relative_path=child_rel,
                        server_relative_path=child_srv,
                        download_url=dl_urls[0] if dl_urls else None,
                        fallback_download_urls=dl_urls[1:],
                        last_modified=row.get("TimeLastModified") or "",
                        backend="rest_v1",
                    )
                )
            file_url = next_url

        return results

    async def _list_render_stream(self, client: httpx.AsyncClient, folder: SPItem) -> list[SPItem]:
        web_url = (self.session.web_url or self.session.origin).rstrip("/")
        digest = self.session.form_digest or await self._fetch_form_digest(client)
        if not digest:
            raise RuntimeError("Missing FormDigestValue for RenderListDataAsStream")

        if self.session.list_id:
            endpoint_base = f"{web_url}/_api/web/Lists(guid'{self.session.list_id}')/RenderListDataAsStream"
        elif self.session.list_url:
            enc_list = encode_sp_path_param(self.session.list_url)
            endpoint_base = f"{web_url}/_api/web/GetListUsingPath(decodedurl=@a1)/RenderListDataAsStream?@a1='{enc_list}'"
        else:
            raise RuntimeError("Missing list_id/list_url for RenderListDataAsStream")

        results: list[SPItem] = []
        paging_qs = "TryNewExperienceSingle=TRUE"
        while True:
            sep = "&" if "?" in endpoint_base else "?"
            url = f"{endpoint_base}{sep}{paging_qs}"
            body = {
                "parameters": {
                    "RenderOptions": 2,
                    "FolderServerRelativeUrl": folder.server_relative_path,
                    "AllowMultipleValueFilterForTaxonomyFields": True,
                }
            }
            code, data = await self._request_json(
                client,
                "POST",
                url,
                json_body=body,
                extra_headers={
                    "Accept": "application/json;odata=verbose",
                    "Content-Type": "application/json;odata=verbose",
                    "X-RequestDigest": digest,
                },
            )
            if code != 200 or not isinstance(data, dict):
                raise RuntimeError(f"RenderListDataAsStream status {code}")

            rows = data.get("Row") or []
            for row in rows:
                name = row.get("FileLeafRef") or ""
                if not name:
                    continue
                is_folder = str(row.get("FSObjType")) == "1"
                child_srv = row.get("FileRef") or f"{(folder.server_relative_path or '').rstrip('/')}/{name}"
                child_rel = f"{folder.relative_path}/{name}".lstrip("/")

                raw_size = row.get("File_x0020_Size") or row.get("SMTotalFileStreamSize") or ""
                size = 0 if is_folder else (int(raw_size) if str(raw_size).isdigit() else -1)

                sp_item_url = row.get(".spItemUrl") or ""
                d_id, i_id = None, None
                m = re.search(r"/_api/v2\.0/drives/([^/]+)/items/([^/?]+)", sp_item_url, re.IGNORECASE)
                if m:
                    d_id, i_id = m.group(1), m.group(2)

                dl_urls = _build_rest_download_urls(web_url, child_srv)
                if d_id and i_id and not is_folder:
                    dl_urls.insert(0, f"{web_url}/_api/v2.0/drives/{d_id}/items/{i_id}/content")

                results.append(
                    SPItem(
                        id=row.get("UniqueId") or child_srv,
                        name=name,
                        is_folder=is_folder,
                        size=size,
                        relative_path=child_rel,
                        server_relative_path=child_srv,
                        drive_id=d_id,
                        item_id=i_id,
                        download_url=dl_urls[0] if (dl_urls and not is_folder) else None,
                        fallback_download_urls=dl_urls[1:] if not is_folder else [],
                        last_modified=row.get("Modified.") or row.get("Modified") or "",
                        backend="render_list",
                    )
                )

            next_href = data.get("NextHref")
            if not next_href:
                break
            paging_qs = next_href.lstrip("?")

        return results

    async def scan_selected_items(
        self,
        selected_items: list[dict[str, Any]],
        on_progress: Optional[Callable[[int, int, int, str], Awaitable[None] | None]] = None,
    ) -> dict[str, Any]:
        """
        Recursively walk all selected folders (and any directly selected files)
        to build a flat deduplicated manifest of all folders & files with exact paths and sizes.
        """
        folders_map: dict[str, str] = {}
        files_map: dict[str, SPItem] = {}
        queue: asyncio.Queue[SPItem] = asyncio.Queue()

        for raw in selected_items:
            item = SPItem(
                id=raw.get("id", ""),
                name=raw["name"],
                is_folder=bool(raw.get("is_folder", True)),
                size=int(raw.get("size", 0)),
                relative_path=raw.get("relative_path", raw["name"]).strip("/"),
                server_relative_path=raw.get("server_relative_path"),
                drive_id=raw.get("drive_id"),
                item_id=raw.get("item_id"),
                download_url=raw.get("download_url"),
                fallback_download_urls=list(raw.get("fallback_download_urls") or []),
                last_modified=raw.get("last_modified", ""),
                backend=raw.get("backend", "v2"),
            )
            if item.is_folder:
                if item.relative_path:
                    folders_map[item.relative_path] = item.relative_path
                await queue.put(item)
            else:
                files_map[item.relative_path] = item

        scanned_folders = 0
        total_bytes = sum(max(0, f.size) for f in files_map.values())
        scan_errors: list[dict[str, str]] = []
        visited_folder_keys: set[str] = set()
        lock = asyncio.Lock()

        async with self._new_httpx_client() as client:
            async def worker() -> None:
                nonlocal scanned_folders, total_bytes
                while True:
                    folder = await queue.get()
                    try:
                        visit_key = folder.id or folder.relative_path or folder.name
                        async with lock:
                            already_visited = visit_key in visited_folder_keys
                            if not already_visited:
                                visited_folder_keys.add(visit_key)

                        if not already_visited:
                            try:
                                children = await self._list_children_with_client(client, folder)
                                for child in children:
                                    if child.is_folder:
                                        async with lock:
                                            if child.relative_path:
                                                folders_map[child.relative_path] = child.relative_path
                                        await queue.put(child)
                                    else:
                                        async with lock:
                                            if child.relative_path not in files_map:
                                                files_map[child.relative_path] = child
                                                if child.size > 0:
                                                    total_bytes += child.size
                            except Exception as exc:
                                async with lock:
                                    scan_errors.append(
                                        {"folder": folder.relative_path or folder.name, "error": str(exc)}
                                    )
                            finally:
                                async with lock:
                                    scanned_folders += 1
                                    if on_progress:
                                        res = on_progress(
                                            scanned_folders,
                                            len(files_map),
                                            total_bytes,
                                            folder.relative_path or folder.name,
                                        )
                                        if asyncio.iscoroutine(res):
                                            await res
                    finally:
                        queue.task_done()

            workers = [asyncio.create_task(worker()) for _ in range(4)]
            await queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        ordered_folders = sorted(folders_map.values())
        ordered_files = sorted(files_map.values(), key=lambda f: f.relative_path.lower())

        return {
            "folders": ordered_folders,
            "files": [f.to_dict() for f in ordered_files],
            "total_folders": len(ordered_folders),
            "total_files": len(ordered_files),
            "total_bytes": total_bytes,
            "scan_errors": scan_errors,
        }

    async def refresh_download_url(self, file_entry: dict[str, Any]) -> tuple[Optional[str], list[str]]:
        """Re-acquire fresh pre-authenticated download URL for a file (when tempauth expires)."""
        drive_id = file_entry.get("drive_id")
        item_id = file_entry.get("item_id")
        srv_path = file_entry.get("server_relative_path")
        base = self._api_base()

        async with self._new_httpx_client() as client:
            if drive_id and item_id and base:
                url = f"{base}/_api/v2.0/drives/{drive_id}/items/{item_id}"
                code, data = await self._request_json(client, "GET", url)
                if code == 200 and isinstance(data, dict):
                    fresh = data.get("@content.downloadUrl") or data.get("@microsoft.graph.downloadUrl")
                    content_url = f"{base}/_api/v2.0/drives/{drive_id}/items/{item_id}/content"
                    fallbacks = [content_url]
                    if srv_path:
                        fallbacks.extend(_build_rest_download_urls(base, srv_path))
                    return fresh or content_url, fallbacks

            if srv_path and base:
                urls = _build_rest_download_urls(base, srv_path)
                return (urls[0] if urls else None), urls[1:]

        return file_entry.get("download_url"), list(file_entry.get("fallback_download_urls") or [])
