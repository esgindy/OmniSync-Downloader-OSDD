"""Desktop toast + optional Telegram notifications (FEAT4)."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path(__file__).resolve().parent / "osdd_settings.json"

DEFAULT_NOTIFY = {"toast": True, "telegram_enabled": False, "telegram_token": "", "telegram_chat_id": ""}


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_notify_config() -> dict:
    cfg = dict(DEFAULT_NOTIFY)
    cfg.update(load_settings().get("notify") or {})
    return cfg


def public_config() -> dict:
    """Config safe to send to the UI (token never returned)."""
    cfg = get_notify_config()
    return {
        "toast": bool(cfg["toast"]),
        "telegram_enabled": bool(cfg["telegram_enabled"]),
        "telegram_chat_id": str(cfg["telegram_chat_id"]),
        "has_token": bool(cfg["telegram_token"]),
    }


def update_notify_config(body: dict) -> dict:
    settings = load_settings()
    cfg = get_notify_config()
    if "toast" in body:
        cfg["toast"] = bool(body["toast"])
    if "telegram_enabled" in body:
        cfg["telegram_enabled"] = bool(body["telegram_enabled"])
    if "telegram_chat_id" in body:
        cfg["telegram_chat_id"] = str(body["telegram_chat_id"] or "").strip()
    if body.get("clear_token"):
        cfg["telegram_token"] = ""
    elif str(body.get("telegram_token") or "").strip():
        cfg["telegram_token"] = str(body["telegram_token"]).strip()
    settings["notify"] = cfg
    save_settings(settings)
    return public_config()


def _toast(title: str, msg: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        from winotify import Notification  # type: ignore

        Notification(app_id="OmniSync", title=title, msg=msg).show()
        return True
    except Exception:
        pass
    try:
        t = title.replace("'", "''")
        m = msg.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
            f"$n.ShowBalloonTip(5000,'{t}','{m}',[System.Windows.Forms.ToolTipIcon]::Info);"
            "Start-Sleep -Seconds 6;$n.Dispose()"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception:
        return False


def _telegram(token: str, chat_id: str, text: str) -> str:
    """Returns '' on success or a short error (never contains the token)."""
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return "" if resp.status == 200 else f"HTTP {resp.status}"
    except Exception as exc:
        return type(exc).__name__


def notify(title: str, msg: str) -> dict:
    """Blocking; call from a thread. Returns per-channel result."""
    cfg = get_notify_config()
    res: dict[str, Any] = {}
    if cfg["toast"]:
        res["toast"] = _toast(title, msg)
    if cfg["telegram_enabled"] and cfg["telegram_token"] and cfg["telegram_chat_id"]:
        err = _telegram(cfg["telegram_token"], cfg["telegram_chat_id"], f"{title}\n{msg}")
        res["telegram"] = not err
        if err:
            res["telegram_error"] = err
    return res
