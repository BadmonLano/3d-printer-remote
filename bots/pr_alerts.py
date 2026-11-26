"""Alerts: owner_inbox packet + optional Telegram (existing adapter) + optional webhooks (part of the printer_remote CLI).

Events: done, failed, anomaly, approval_needed, filament_low, maintenance_due.
Env (names only; values live in .env):
  ALERT_WEBHOOK_URLS     comma list of URLs (blank = no webhooks)
  ALERT_WEBHOOK_FORMAT   json (default) | slack | discord
  ALERT_WEBHOOK_SECRET   optional; adds X-TRM-Signature: sha256=<hmac of body>
  ALERT_EVENTS           comma list to send (default all)
  ALERT_TELEGRAM         true (default) sends through the existing Telegram adapter when its token/chat are set
  ALERT_DEDUP_MIN        same event+printer+key is sent at most once per N minutes (default 15)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

import printer_remote as core

EVENTS = ("done", "failed", "anomaly", "approval_needed", "filament_low", "maintenance_due")


def _state_path():
    return core.state_dir() / "alerts_state.json"


def _wanted(event: str) -> bool:
    lst = [e.strip() for e in core.env("ALERT_EVENTS").split(",") if e.strip()]
    return not lst or event in lst


def webhook_payload(event: str, text: str, data: dict, printer: str | None) -> dict:
    fmt = (core.env("ALERT_WEBHOOK_FORMAT") or "json").lower()
    line = f"[3D Printer Remote] {event}" + (f" ({printer})" if printer else "") + f": {text}"
    if fmt == "slack":
        return {"text": line}
    if fmt == "discord":
        return {"content": line[:1900]}
    return {"source": "3d-printer-remote", "version": core.VERSION, "event": event, "printer": printer, "text": text,
            "data": data, "at": core.ts_human()}


def post_webhook(url: str, body: dict, event: str) -> dict:
    raw = json.dumps(body, default=str).encode()
    headers = {"Content-Type": "application/json", "User-Agent": f"3d-printer-remote/{core.VERSION}", "X-TRM-Event": event}
    sec = core.env("ALERT_WEBHOOK_SECRET")
    if sec:
        headers["X-TRM-Signature"] = "sha256=" + hmac.new(sec.encode(), raw, hashlib.sha256).hexdigest()
    last = None
    for delay in [0.0] + core.backoff_delays(int(core.env_float("ALERT_WEBHOOK_RETRIES", 2)), 0.5):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=core.env_float("ALERT_WEBHOOK_TIMEOUT_S", 5)) as r:
                return {"ok": 200 <= r.status < 300, "status": r.status}
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if 400 <= e.code < 500 and e.code != 429:
                break  # client error: retrying will not help
        except OSError as e:
            last = str(e)[:120]
    return {"ok": False, "error": last}


def emit(event: str, text: str, data: dict | None = None, printer: str | None = None, photo: str | None = None,
         key: str = "", telegram: bool = True, priority: str | None = None) -> dict:
    if event not in EVENTS:
        raise core.Fail(f"unknown alert event {event}; use one of {EVENTS}")
    data = data or {}
    res: dict = {"event": event, "printer": printer, "sent": False}
    if not _wanted(event):
        res["skipped"] = "not in ALERT_EVENTS"
        return res
    st = core._read_json(_state_path()) or {}
    dk = f"{event}|{printer}|{key}"
    now = time.time()
    if now - float(st.get(dk, 0)) < core.env_float("ALERT_DEDUP_MIN", 15) * 60 and event != "approval_needed":
        res["skipped"] = "duplicate within ALERT_DEDUP_MIN"
        return res
    st[dk] = now
    core.atomic_write(_state_path(), json.dumps(st))
    prio = priority or ("high" if event in ("failed", "anomaly", "approval_needed") else "normal")
    if event != "approval_needed":  # request_gated already wrote the approval packet
        res["packet"] = str(core.write_packet(f"alert {event}", "alert", text, {"event": event, "printer": printer, **data},
                                              event, priority=prio))
    if telegram and core.env_bool("ALERT_TELEGRAM", True) and core.env("TELEGRAM_BOT_TOKEN") and core.env("TELEGRAM_CHAT_ID"):
        res["telegram"] = core.telegram_send(f"{event}" + (f" ({printer})" if printer else "") + f": {text}", photo)
    hooks = [u.strip() for u in core.env("ALERT_WEBHOOK_URLS").split(",") if u.strip()]
    if hooks:
        body = webhook_payload(event, text, data, printer)
        res["webhooks"] = [post_webhook(u, body, event) for u in hooks]
    res["sent"] = True
    log = core.load_store("alerts_log", [])
    log.append({"at": core.ts_human(), "event": event, "printer": printer, "text": text[:300],
                "webhooks_ok": [w.get("ok") for w in res.get("webhooks", [])]})
    core.save_store("alerts_log", log[-500:])
    return res


def cmd_alerts(a, out):
    if a.alerts_cmd == "test":
        out["alert"] = emit(a.event, a.text or f"test {a.event} alert", {"test": True}, printer=a.printer_name,
                            key=str(time.time()))
    elif a.alerts_cmd == "list":
        out["alerts"] = core.load_store("alerts_log", [])[-a.limit:]
    out["events"] = EVENTS
    out["webhooks_configured"] = len([u for u in core.env("ALERT_WEBHOOK_URLS").split(",") if u.strip()])


def register(sp, handlers: dict) -> None:
    x = sp.add_parser("alerts", help="webhook + Telegram alerts: test, list")
    s = x.add_subparsers(dest="alerts_cmd", required=True)
    y = s.add_parser("test")
    y.add_argument("--event", default="done", choices=EVENTS)
    y.add_argument("--text")
    y.add_argument("--printer", dest="printer_name")
    y = s.add_parser("list")
    y.add_argument("--limit", type=int, default=50)
    handlers["alerts"] = cmd_alerts
