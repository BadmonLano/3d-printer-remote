"""Multi-printer fleet: config, per-printer targeting, fleet status/stream, read-only LAN discovery.

Config (both gitignored; examples ship): printers.json (or printers.yaml with PyYAML), or PR_FLEET_FILE.
  {"printers": [{"name": "se1", "model": "CR-10 SE", "nozzle_mm": 0.4, "material": "PETG", "color": "white",
                 "bed_mm": [220, 220, 250], "groups": ["garage"], "tags": ["toys"], "ip_env": "PRINTER_IP_SE1",
                 "cameras": [{"name": "top", "url_env": "CAMERA_URL_SE1"}], "backend": "creality_ws",
                 "maintenance": {"nozzle_check": 200}}]}
IPs never go in tracked files: use `ip_env` (an env var in .env) or printers.local.json {"se1": "<ip>"}.
No fleet file = the classic single printer (PRINTER_IP), exactly as in 1.x.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import printer_remote as core

DEFAULT_BED = [220, 220, 250]


def fleet_path() -> Path | None:
    if core.env("PR_FLEET_FILE"):
        return Path(core.env("PR_FLEET_FILE"))
    for n in ("printers.json", "printers.yaml", "printers.yml"):
        p = core.ROOT / n
        if p.is_file():
            return p
    return None


def local_ip_path() -> Path:
    return Path(core.env("PR_FLEET_LOCAL_FILE") or (core.ROOT / "printers.local.json"))


def _load_file(p: Path):
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # optional
        except ImportError:
            raise core.Fail(f"{p.name} needs PyYAML (pip install pyyaml), or use printers.json")
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return json.loads(p.read_text(encoding="utf-8") or "{}")


def load_fleet() -> list[dict]:
    p = fleet_path()
    if not p or not p.is_file():
        return []
    try:
        j = _load_file(p)
    except ValueError as e:
        raise core.Fail(f"{p.name} is not valid: {e}")
    rows = j.get("printers") if isinstance(j, dict) else j
    out, seen = [], set()
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        n = str(r["name"])
        if n in seen:
            raise core.Fail(f"duplicate printer name {n} in {p.name}")
        seen.add(n)
        r = dict(r)
        r.setdefault("bed_mm", DEFAULT_BED)
        r.setdefault("nozzle_mm", 0.4)
        r.setdefault("groups", [])
        r.setdefault("tags", [])
        r.setdefault("backend", "creality_ws")
        out.append(r)
    return out


def save_fleet(rows: list[dict]) -> Path:
    p = fleet_path() or (core.ROOT / "printers.json")
    if p.suffix.lower() in (".yaml", ".yml"):
        raise core.Fail("editing printers.yaml from the CLI is not supported; edit it by hand or switch to printers.json")
    for r in rows:
        if "ip" in r:
            raise core.Fail("keep IPs out of printers.json; use ip_env or printers.local.json")
    core.atomic_write(p, json.dumps({"printers": rows}, indent=2))
    return p


def fleet_configured() -> bool:
    try:
        return bool(load_fleet())
    except core.Fail:
        return False


def default_printer_name() -> str | None:
    rows = load_fleet()
    return rows[0]["name"] if len(rows) == 1 else None


def get_printer(name: str) -> dict | None:
    return next((r for r in load_fleet() if r["name"] == name), None)


def _local_ips() -> dict:
    p = local_ip_path()
    return (core._read_json(p) or {}) if p.is_file() else {}


def resolve_ip(p: dict) -> str | None:
    li = _local_ips().get(p["name"])
    if li:
        return str(li)
    if p.get("ip_env") and core.env(p["ip_env"]):
        return core.env(p["ip_env"])
    return p.get("ip") or None


def cfg_for(p: dict) -> dict:
    ip = resolve_ip(p)
    if not ip and p.get("backend") != "moonraker_url":
        raise core.Fail(f"no IP for printer {p['name']}: set {p.get('ip_env') or 'ip_env'} in .env or add it to printers.local.json")
    return {"name": p["name"], "ip": ip, "http_port": int(p.get("http_port") or core.env("PRINTER_HTTP_PORT") or 80),
            "ws_port": int(p.get("ws_port") or core.env("PRINTER_WS_PORT") or 9999),
            "gcode_root": p.get("gcode_root") or core.env("PRINTER_GCODE_ROOT") or "/usr/data/printer_data/gcodes",
            "backend": p.get("backend") or "creality_ws", "moonraker_port": int(p.get("moonraker_port") or 7125),
            "moonraker_api_key_env": p.get("moonraker_api_key_env")}


def cfg_for_name(name: str) -> dict:
    p = get_printer(name)
    if not p:
        if name == "default" and not load_fleet():
            core.set_active_printer(None)
            return core.printer_cfg()
        names = [r["name"] for r in load_fleet()]
        raise core.Fail(f"unknown printer {name!r}; fleet has {names or 'no printers.json'}")
    return cfg_for(p)


def camera_url_for(name: str | None, camera: str | None) -> str | None:
    rows = [get_printer(name)] if name else load_fleet()
    for p in rows:
        if not p:
            continue
        cams = p.get("cameras") or []
        pick = [c for c in cams if not camera or c.get("name") == camera]
        for c in pick:
            u = (core.env(c["url_env"]) if c.get("url_env") else "") or c.get("url")
            if u:
                return u
    return None


def printers() -> list[dict]:
    """Fleet printers, or one implicit `default` printer built from the classic env (1.x behavior)."""
    rows = load_fleet()
    if rows:
        return rows
    return [{"name": "default", "implicit": True, "model": core.env("PRINTER_MODEL") or "CR-10 SE",
             "nozzle_mm": float(core.env("NOZZLE_MM") or 0.4), "material": core.env("FILAMENT_MATERIAL") or None,
             "color": core.env("LOADED_COLOR") or None,
             "bed_mm": [float(core.env("BED_X_MM") or 220), float(core.env("BED_Y_MM") or 220), float(core.env("MAX_Z_MM") or 250)],
             "groups": [], "tags": [], "backend": "creality_ws"}]


def _ctx_name(p: dict) -> str | None:
    return None if p.get("implicit") else p["name"]


def printer_snapshot(p: dict, wait: float = 3.0) -> dict:
    """Read-only status of one printer + availability verdict (thread-safe: sets the thread's active printer)."""
    core.set_active_printer(_ctx_name(p))
    row = {"name": p["name"], "model": p.get("model"), "nozzle_mm": p.get("nozzle_mm"), "material": p.get("material"),
           "color": p.get("color"), "groups": p.get("groups"), "tags": p.get("tags"), "backend": p.get("backend"),
           "bed_mm": p.get("bed_mm")}
    t0 = time.monotonic()
    try:
        cfg = core.printer_cfg()
        st = core.printer_status(cfg, wait=wait)
        row.update(reachable=True, state=st.get("state"), printing=st.get("printing"), paused=st.get("paused"),
                   idle=st.get("idle"), file=st.get("file"), progress_pct=st.get("progress_pct"),
                   remaining_s=st.get("remaining_s"), nozzle=st.get("nozzle"), bed=st.get("bed"),
                   has_error=st.get("has_error"), error=st.get("error"),
                   power_loss_recovery=st.get("power_loss_recovery"), firmware=st.get("firmware_version"))
    except Exception as e:
        row.update(reachable=False, error=str(e)[:200])
    row["read_ms"] = int((time.monotonic() - t0) * 1000)
    killed, why = core._mod("pr_policy").is_killed(_ctx_name(p))
    row["killed"], row["kill_reason"] = killed, why
    reasons = []
    if not row.get("reachable"):
        reasons.append("unreachable")
    else:
        if not row.get("idle"):
            reasons.append(f"busy ({row.get('state')})")
        if row.get("has_error"):
            reasons.append("printer error")
        if row.get("power_loss_recovery") not in (None, 0, "0", ""):
            reasons.append("power-loss record pending")
    if killed:
        reasons.append("kill switch")
    try:
        due = core._mod("pr_ops").maint_due_blocking(p["name"])
        if due:
            reasons.append(f"maintenance due: {', '.join(due)}")
    except Exception:
        pass
    row["available"] = not reasons
    row["unavailable_reasons"] = reasons
    return row


def fleet_status(names: list[str] | None = None, wait: float = 3.0) -> list[dict]:
    rows = [p for p in printers() if not names or p["name"] in names]
    if not rows:
        raise core.Fail("no matching printers")
    with ThreadPoolExecutor(max_workers=min(16, len(rows))) as ex:
        return list(ex.map(lambda p: printer_snapshot(p, wait), rows))


# ---------------------------------------------------------------- discovery (read-only)
def _local_subnet() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # TEST-NET address; no packet is sent for UDP connect
        ip = s.getsockname()[0]
        s.close()
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    except OSError:
        return None


def discover(subnet: str | None, port: int, timeout: float, confirm: bool = True, max_hosts: int = 1024) -> dict:
    subnet = subnet or _local_subnet()
    if not subnet:
        raise core.Fail("could not work out the local subnet; pass --subnet 192.0.2.0/24")
    net = ipaddress.ip_network(subnet, strict=False)
    hosts = [str(h) for h in (net.hosts() if net.num_addresses > 1 else [net.network_address])]
    if len(hosts) > max_hosts:
        raise core.Fail(f"{subnet} has {len(hosts)} hosts; limit is {max_hosts} (use a /22 or smaller)")

    def probe(ip):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return ip
        except OSError:
            return None
    with ThreadPoolExecutor(max_workers=64) as ex:
        open_hosts = [ip for ip in ex.map(probe, hosts) if ip]
    found = []
    for ip in open_hosts:
        row = {"ip": ip, "port": port, "creality_os": None}
        if confirm:
            try:
                st = core.printer_status({"ip": ip, "ws_port": port, "http_port": 80}, wait=2.5)
                row.update(creality_os=True, model=st.get("model"), hostname=st.get("hostname"),
                           firmware=st.get("firmware_version"), state=st.get("state"))
            except Exception as e:
                row.update(creality_os=False, note=str(e)[:120])
        found.append(row)
    return {"subnet": subnet, "scanned": len(hosts), "port": port, "found": found}


# ---------------------------------------------------------------- CLI
def cmd_fleet(a, out):
    c = a.fleet_cmd
    if c == "list":
        rows = printers()
        out["fleet_file"] = str(fleet_path()) if fleet_path() else None
        out["printers"] = [{**{k: v for k, v in r.items() if k != "ip"}, "ip_configured": bool(resolve_ip(r))
                            if not r.get("implicit") else bool(core.discover_printer_ip())} for r in rows]
        out["count"] = len(rows)
    elif c == "status":
        names = [n for n in (a.names or "").split(",") if n] or None
        rows = fleet_status(names, wait=a.wait)
        out["printers"] = rows
        out["summary"] = {"total": len(rows), "reachable": sum(1 for r in rows if r.get("reachable")),
                          "printing": sum(1 for r in rows if r.get("printing") and not r.get("paused")),
                          "paused": sum(1 for r in rows if r.get("paused")),
                          "available": [r["name"] for r in rows if r.get("available")],
                          "errors": [r["name"] for r in rows if r.get("has_error")],
                          "unreachable": [r["name"] for r in rows if not r.get("reachable")]}
    elif c == "stream":
        rows = [p for p in printers() if not a.names or p["name"] in a.names.split(",")]
        lock = threading.Lock()
        results: dict = {}

        def run(p):
            core.set_active_printer(_ctx_name(p))
            try:
                cfg = core.printer_cfg()

                def cb(norm):
                    if a.jsonl:
                        with lock:
                            sys.stdout.write(json.dumps({"type": "update", "printer": p["name"], **norm}, default=str) + "\n")
                            sys.stdout.flush()
                results[p["name"]] = core.printer_stream(cfg, a.seconds, False, a.min_interval, on_update=cb)
            except Exception as e:
                results[p["name"]] = {"error": str(e)[:200]}
        ts = [threading.Thread(target=run, args=(p,), daemon=True) for p in rows]
        for t in ts:
            t.start()
        for t in ts:
            t.join(a.seconds + 30)
        if a.jsonl:
            out["type"] = "summary"
        out["printers"] = {k: {kk: vv for kk, vv in v.items() if kk != "latest"} | {"state": (v.get("latest") or {}).get("state")}
                           for k, v in results.items()}
    elif c == "discover":
        r = discover(a.subnet, a.port, a.timeout, confirm=not a.no_confirm)
        out.update(r)
        out["note"] = "read-only: TCP connect + one status read per open host; nothing is sent to change any printer"
        if a.save:
            li = _local_ips()
            rows = load_fleet()
            names = {x["name"] for x in rows}
            added = []
            for i, f in enumerate([f for f in r["found"] if f.get("creality_os")]):
                if f["ip"] in li.values():
                    continue
                base = core.safe_name(str(f.get("hostname") or "printer")).lower().replace(" ", "-")
                n, k = base, 2
                while n in names:
                    n, k = f"{base}-{k}", k + 1
                names.add(n)
                li[n] = f["ip"]
                rows.append({"name": n, "model": f.get("model"), "nozzle_mm": 0.4, "material": None, "color": None,
                             "bed_mm": DEFAULT_BED, "groups": [], "tags": ["discovered"]})
                added.append(n)
            if added:
                core.atomic_write(local_ip_path(), json.dumps(li, indent=2))
                save_fleet(rows)
            out["saved"] = added
            out["note"] += f"; IPs saved only to {local_ip_path().name} (gitignored)"
    elif c in ("add", "set"):
        rows = load_fleet()
        cur = next((r for r in rows if r["name"] == a.name), None)
        if c == "add" and cur:
            raise core.Fail(f"printer {a.name} already exists; use `fleet set`")
        if c == "set" and not cur:
            raise core.Fail(f"no printer {a.name}")
        if not cur:
            cur = {"name": a.name, "nozzle_mm": 0.4, "bed_mm": DEFAULT_BED, "groups": [], "tags": []}
            rows.append(cur)
        for k in ("model", "material", "color", "ip_env", "backend"):
            v = getattr(a, k, None)
            if v is not None:
                cur[k] = (v.upper() if k == "material" else v.lower() if k == "color" else v) or None
        if a.nozzle is not None:
            cur["nozzle_mm"] = a.nozzle
        if a.bed:
            cur["bed_mm"] = [float(x) for x in a.bed.lower().split("x")]
        if a.groups is not None:
            cur["groups"] = [g for g in a.groups.split(",") if g]
        if a.tags is not None:
            cur["tags"] = [g for g in a.tags.split(",") if g]
        if a.ip:
            li = _local_ips()
            li[a.name] = a.ip
            core.atomic_write(local_ip_path(), json.dumps(li, indent=2))
            out["ip_saved_to"] = local_ip_path().name
        for r in rows:
            r.pop("ip", None)
        out["fleet_file"] = str(save_fleet(rows))
        out["printer"] = cur
        core._mod("pr_policy").audit(f"fleet_{c}", printer=a.name, detail={k: cur.get(k) for k in ("material", "color", "nozzle_mm")})
    elif c == "remove":
        rows = [r for r in load_fleet() if r["name"] != a.name]
        save_fleet(rows)
        out["removed"] = a.name


def register(sp, handlers: dict) -> None:
    x = sp.add_parser("fleet", help="multi-printer: list, status, stream, discover (read-only), add/set/remove")
    fs = x.add_subparsers(dest="fleet_cmd", required=True)
    fs.add_parser("list")
    y = fs.add_parser("status", help="one read-only status per printer, in parallel, with an availability verdict")
    y.add_argument("--names", help="comma list (default all)")
    y.add_argument("--wait", type=float, default=3.0)
    y = fs.add_parser("stream", help="live websocket stream for every printer (reconnects on drops)")
    y.add_argument("--names")
    y.add_argument("--seconds", type=float, default=30)
    y.add_argument("--jsonl", action="store_true")
    y.add_argument("--min-interval", dest="min_interval", type=float, default=1.0)
    y = fs.add_parser("discover", help="scan the LAN for Creality OS (port 9999), read-only")
    y.add_argument("--subnet", help="e.g. 192.0.2.0/24 (default: this machine's /24)")
    y.add_argument("--port", type=int, default=9999)
    y.add_argument("--timeout", type=float, default=0.4)
    y.add_argument("--no-confirm", dest="no_confirm", action="store_true", help="skip the status read on open hosts")
    y.add_argument("--save", action="store_true", help="add found printers (IPs go to printers.local.json only)")
    for n in ("add", "set"):
        y = fs.add_parser(n, help=f"{n} a printer in printers.json")
        y.add_argument("name")
        y.add_argument("--model")
        y.add_argument("--nozzle", type=float)
        y.add_argument("--material")
        y.add_argument("--color")
        y.add_argument("--bed", help="XxYxZ mm, e.g. 220x220x250")
        y.add_argument("--groups")
        y.add_argument("--tags")
        y.add_argument("--ip-env", dest="ip_env", help="env var holding the IP (kept in .env)")
        y.add_argument("--ip", help="stored only in printers.local.json (gitignored)")
        y.add_argument("--backend", choices=("creality_ws", "moonraker"))
    y = fs.add_parser("remove")
    y.add_argument("name")
    handlers["fleet"] = cmd_fleet
