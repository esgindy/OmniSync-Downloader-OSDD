"""Tiny status tracker for the v6 build. Zero dependencies, zero tokens.

Usage (agents and humans):
  python dev_dashboard/dev_status.py set <TASK_ID> <todo|doing|done|blocked> [--agent NAME] [--note TEXT]
  python dev_dashboard/dev_status.py log "message" [--agent NAME]
  python dev_dashboard/dev_status.py render      # rebuild status.js from status.json

Each call rewrites status.json and status.js. dashboard index.html re-loads status.js every 3s.
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
JSON_F = HERE / "status.json"
JS_F = HERE / "status.js"

DEFAULT = {
    "version": "6.0.0",
    "tasks": [
        # id, group, title
        ["SETUP1", "setup", "إنشاء فرع v6.0 وتنظيف الملفات المؤقتة"],
        ["SETUP2", "setup", "داش بورد المتابعة"],
        ["SEC1", "security", "فحص Origin/Host + token لكل الـ API"],
        ["SEC2", "security", "إيقاف آمن (graceful) بدل os._exit"],
        ["PERF1", "performance", "رفع حدود httpx + HTTP/2"],
        ["PERF2", "performance", "Backoff أسي + Retry-After + 429"],
        ["PERF3", "performance", "تحميل الملف الكبير بأجزاء Range متوازية"],
        ["PERF4", "performance", "حد سرعة (Bandwidth limiter)"],
        ["SYNC1", "sync", "مزامنة: حجم+وقت أولاً ثم hash للمشكوك فقط"],
        ["SYNC2", "sync", "Dry-run + اكتشاف التكرارات"],
        ["UI1", "ui", "ثيمات (5) عبر CSS variables + منتقي ثيم"],
        ["UI2", "ui", "تبويبات + لوحة مؤشرات + workers + sparkline"],
        ["UI3", "ui", "إزالة style= المضمّنة وإصلاح dashDestDir"],
        ["FEAT1", "features", "فلاتر (امتداد/حجم/تاريخ/regex)"],
        ["FEAT2", "features", "طابور مهام متعدد + أولويات"],
        ["FEAT3", "features", "جدولة المزامنة"],
        ["FEAT4", "features", "إشعارات ويندوز/Telegram"],
        ["FEAT5", "features", "سجل مهام SQLite + حفظ الحالة"],
        ["QA1", "quality", "تثبيت requirements + ruff + CI"],
        ["QA2", "quality", "اختبارات (unittest) للميزات الجديدة"],
        ["DOC1", "docs", "رفع الإصدار 6.0.0 + README"],
        ["DOC2", "docs", "تحديث PROJECT_HANDOVER_SUMMARY.md"],
    ],
}


def load():
    if JSON_F.exists():
        return json.loads(JSON_F.read_text(encoding="utf-8"))
    data = {"version": DEFAULT["version"], "tasks": [], "log": []}
    for tid, grp, title in DEFAULT["tasks"]:
        data["tasks"].append({"id": tid, "group": grp, "title": title,
                              "status": "todo", "agent": "", "note": "", "updated": ""})
    return data


def save(data):
    data["generated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    JSON_F.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    JS_F.write_text("window.OSDD_STATUS = " + json.dumps(data, ensure_ascii=False) + ";\n",
                    encoding="utf-8")


def arg(flag, default=""):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    data = load()
    data.setdefault("log", [])
    now = time.strftime("%H:%M:%S")
    if cmd == "set" and len(sys.argv) >= 4:
        tid, st = sys.argv[2], sys.argv[3]
        for t in data["tasks"]:
            if t["id"] == tid:
                t["status"] = st
                t["agent"] = arg("--agent", t["agent"])
                t["note"] = arg("--note", t["note"])
                t["updated"] = now
                data["log"].append({"t": now, "agent": t["agent"], "msg": f"{tid} -> {st} {t['note']}".strip()})
                break
        else:
            print("unknown task", tid)
            return
    elif cmd == "log" and len(sys.argv) >= 3:
        data["log"].append({"t": now, "agent": arg("--agent"), "msg": sys.argv[2]})
    data["log"] = data["log"][-200:]
    save(data)
    print("ok")


if __name__ == "__main__":
    main()
