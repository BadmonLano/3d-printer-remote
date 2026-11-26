#!/usr/bin/env python3
"""3D Printer Remote: AI-agent bridge for Creality Print 7.2 and a Creality CR-10 SE.
NOT YET HARDWARE-VERIFIED ON A CR-10 SE (printer protocol from K1-family firmware; see docs/LIMITS.md).

One consolidated CLI. Every subcommand prints ONE JSON object on stdout:
    {"status": "ok|error|awaiting_owner|declined|preflight_failed|skipped|alert", "job_id": "...", "command": "...",
     "errors": [...], "warnings": [...], ...data}

Free (no confirm): detect, presets, slice, open, split-check, gcode inspect, queue/history/filament/maint/library,
                   recommend, plates, timelapse, health, printer discover/probe/status/stream/latest/info/files/...,
                   printer upload, read-only gcode queries, camera, watch (observe + alert).
Gated (owner yes in chat): printer start / pause / resume / cancel / heat / home / move / fan / non-read-only gcode.
    First call -> request + summary, status "awaiting_owner". The owner answers YES/NO in chat.
    `approve <request_id> --yes|--no` records it; on yes an automatic preflight runs and the action executes
    only if preflight passes. Agents pass along only the owner's own answer, never their own.

Stdlib only (Pillow/numpy optional for camera features).
Ideas credited in docs/CREDITS.md (marcmayol/claude-code-creality-print-plugin, sairaph/creality-slicer-mcp; both MIT).
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

VERSION = "1.1.0"
BOT = "printer_remote"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
IS_WIN = os.name == "nt"
PRESET_TYPES = ("machine", "process", "filament")
DEFAULT_PRINTER = "Creality CR-10 SE 0.4 nozzle"
DEFAULT_PROCESS = "0.20mm Standard @Creality CR-10 SE 0.4 nozzle"
DEFAULT_FILAMENT = "CR-PETG @Creality CR-10 SE 0.4 nozzle"
GATED_ACTIONS = ("start", "pause", "resume", "cancel", "heat", "home", "fan", "move")

# friendly override name -> Creality Print key
OVERRIDE_ALIASES = {
    "layer_height": "layer_height", "walls": "wall_loops", "wall_loops": "wall_loops",
    "infill": "sparse_infill_density", "sparse_infill_density": "sparse_infill_density",
    "infill_pattern": "sparse_infill_pattern", "top_layers": "top_shell_layers",
    "bottom_layers": "bottom_shell_layers", "brim": "brim_width", "brim_width": "brim_width",
    "supports": "enable_support", "enable_support": "enable_support",
}


# ============================================================ env / paths / time
class Fail(RuntimeError):
    """Expected failure -> JSON error, exit 1."""


def load_env() -> None:
    for f in (ROOT / ".env", HERE / ".env"):
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool) -> bool:
    v = env(name)
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name) or default)
    except ValueError:
        return default


def state_dir() -> Path:
    d = Path(env("PR_STATE_DIR") or (HERE / "state"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def owner_inbox_dir() -> Path:
    d = Path(env("PR_OWNER_INBOX") or (ROOT / "owner_inbox"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _nth_sunday(year: int, month: int, n: int) -> _dt.date:
    d = _dt.date(year, month, 1)
    d += _dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + _dt.timedelta(weeks=n - 1)


def now_et() -> _dt.datetime:
    """Current time in America/New_York. zoneinfo when tzdata exists, else US DST rules."""
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        utc = _dt.datetime.now(_dt.timezone.utc)
        y = utc.year
        start = _dt.datetime.combine(_nth_sunday(y, 3, 2), _dt.time(7), _dt.timezone.utc)
        end = _dt.datetime.combine(_nth_sunday(y, 11, 1), _dt.time(6), _dt.timezone.utc)
        off = -4 if start <= utc < end else -5
        return utc.astimezone(_dt.timezone(_dt.timedelta(hours=off), "ET"))


def ts_compact(t: _dt.datetime | None = None) -> str:
    return (t or now_et()).strftime("%Y%m%d_%H%M%S")


def ts_human(t: _dt.datetime | None = None) -> str:
    return (t or now_et()).strftime("%Y-%m-%d %H:%M:%S ET")


def new_job_id() -> str:
    return f"pr-{ts_compact()}-{uuid.uuid4().hex[:6]}"


def atomic_write(path: Path, data: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(tmp, mode, **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as f:
        f.write(data)
    os.replace(tmp, path)
    return path


def safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", s).strip(" .")[:80] or "item"


def write_packet(subject: str, ptype: str, summary: str, output: dict, status: str,
                 priority: str = "normal", lane: str = "printer") -> Path:
    t = now_et()
    pid = f"{BOT}-{ts_compact(t)}-{uuid.uuid4().hex[:6]}"
    pkt = {"id": pid, "type": ptype, "received_at": t.isoformat(timespec="seconds"), "lane": lane,
           "priority": priority, "context": {"summary": summary, "output": output, "status": status}}
    name = f"{BOT} - {safe_name(subject)}_{ts_compact(t)}.json"
    p = owner_inbox_dir() / name
    n = 1
    while p.exists():
        p = owner_inbox_dir() / f"{BOT} - {safe_name(subject)}_{ts_compact(t)}_{n}.json"
        n += 1
    return atomic_write(p, json.dumps(pkt, indent=2))


def log_job(record: dict) -> None:
    try:
        d = state_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "jobs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


# ============================================================ Creality Print install / data
def find_exe() -> Path | None:
    e = env("CREALITY_PRINT_EXE")
    if e:
        p = Path(e)
        return p if p.is_file() else None
    cands = []
    for base in filter(None, [os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), r"C:\Program Files"]):
        b = Path(base) / "Creality"
        if b.is_dir():
            cands += list(b.glob("Creality Print*/CrealityPrint.exe"))
    if not cands:
        return None
    return max(set(cands), key=lambda p: _vtuple(p.parent.name))


def _vtuple(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:4]) or (0,)


def exe_version(exe: Path) -> dict:
    info = {"folder_version": ".".join(map(str, _vtuple(exe.parent.name))) if re.search(r"\d", exe.parent.name) else None,
            "file_version": None}
    if IS_WIN and exe.suffix.lower() == ".exe":
        try:
            ps = f"(Get-Item -LiteralPath '{exe}').VersionInfo | Select-Object ProductVersion,FileVersion | ConvertTo-Json"
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0 and r.stdout.strip():
                d = json.loads(r.stdout)
                info["file_version"] = d.get("FileVersion") or d.get("ProductVersion")
        except Exception:
            pass
    return info


def is_73_plus(exe: Path) -> bool:
    return _vtuple(exe.parent.name) >= (7, 3) if re.search(r"\d", exe.parent.name) else False


def data_dir() -> Path | None:
    e = env("CREALITY_DATA_DIR")
    if e:
        return Path(e) if Path(e).is_dir() else None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = Path(appdata) / "Creality" / "Creality Print"
    if not base.is_dir():
        return None
    vers = [p for p in base.iterdir() if p.is_dir() and re.fullmatch(r"\d+(\.\d+)*", p.name)]
    return max(vers, key=lambda p: _vtuple(p.name)) if vers else None


def bundled_profiles_dir(exe: Path | None) -> Path | None:
    if not exe:
        return None
    p = exe.parent / "resources" / "profiles"
    return p if p.is_dir() else None


def gui_running() -> list[int]:
    if env("PR_FAKE_GUI_RUNNING") == "none":  # tests: pretend no window is open
        return []
    if env("PR_FAKE_GUI_RUNNING"):
        return [int(x) for x in env("PR_FAKE_GUI_RUNNING").split(",") if x.strip().isdigit()] or [1]
    try:
        if IS_WIN:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq CrealityPrint.exe", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            pids = []
            for line in r.stdout.splitlines():
                parts = [x.strip('"') for x in line.split('","')]
                if len(parts) > 1 and parts[0].lower().startswith("crealityprint") and parts[1].isdigit():
                    pids.append(int(parts[1]))
            return pids
        r = subprocess.run(["pgrep", "-f", "CrealityPrint"], capture_output=True, text=True, timeout=5)
        return [int(x) for x in r.stdout.split() if x.isdigit() and int(x) != os.getpid()]
    except Exception:
        return []


# ============================================================ presets
def preset_roots() -> dict:
    """{'system': [dirs], 'user': [dirs], 'bundled': [dirs]} each containing machine/process/filament."""
    dd = data_dir()
    roots = {"system": [], "user": [], "bundled": []}
    if dd:
        sysd = dd / "system"
        if sysd.is_dir():
            roots["system"] = [p for p in sysd.iterdir() if p.is_dir()]
        ud = dd / "user"
        if ud.is_dir():
            roots["user"] = [p for p in ud.iterdir() if p.is_dir() and p.name.lower() != "temp"]
    b = bundled_profiles_dir(find_exe())
    if b:
        roots["bundled"] = [p for p in b.iterdir() if p.is_dir()]
    return roots


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def index_presets() -> dict:
    """name -> {path, type, source, from_, inherits} (user overrides system overrides bundled)."""
    idx: dict = {}
    roots = preset_roots()
    for source in ("bundled", "system", "user"):
        for r in roots[source]:
            for t in PRESET_TYPES:
                d = r / t
                if not d.is_dir():
                    continue
                for f in d.rglob("*.json"):
                    j = _read_json(f)
                    if not isinstance(j, dict):
                        continue
                    name = j.get("name") or f.stem
                    idx[name] = {"name": name, "type": j.get("type") or t, "source": source,
                                 "path": str(f), "inherits": j.get("inherits"),
                                 "instantiation": j.get("instantiation"), "account_folder": r.name}
    return idx


def find_preset(name: str, idx: dict | None = None, ptype: str | None = None) -> dict:
    idx = idx if idx is not None else index_presets()
    if name in idx and (not ptype or idx[name]["type"] == ptype):
        return idx[name]
    low = name.lower()
    hits = [v for k, v in idx.items() if low in k.lower() and (not ptype or v["type"] == ptype)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise Fail(f"preset not found: {name!r}")
    raise Fail(f"preset name {name!r} is ambiguous: " + "; ".join(sorted(h["name"] for h in hits)[:12]))


def resolve_preset(name: str, idx: dict | None = None, ptype: str | None = None, depth: int = 0) -> dict:
    if depth > 20:
        raise Fail("preset inheritance loop")
    idx = idx if idx is not None else index_presets()
    meta = find_preset(name, idx, ptype)
    j = _read_json(Path(meta["path"])) or {}
    parent = j.get("inherits")
    merged = {}
    if parent:
        try:
            merged = resolve_preset(parent, idx, meta["type"] if meta["type"] in PRESET_TYPES else None, depth + 1)
        except Fail:
            merged = {}
    merged.update(j)
    merged.pop("inherits", None)
    return merged


def coerce_value(old, new: str):
    """Keep the preset's value shape: list stays list, strings stay strings."""
    if isinstance(old, list):
        return [str(new)] * max(1, len(old)) if not str(new).startswith("[") else json.loads(new)
    return str(new)


def normalize_override(key: str, val: str) -> tuple[str, str]:
    k = OVERRIDE_ALIASES.get(key, key)
    v = str(val)
    if k == "sparse_infill_density" and not v.endswith("%"):
        v = v + "%"
    if k == "enable_support":
        v = "1" if v.lower() in ("1", "true", "yes", "on") else "0"
    return k, v


def parse_sets(pairs: list[str] | None) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise Fail(f"--set expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        k, v = normalize_override(k.strip(), v.strip())
        out[k] = v
    return out


def backup_file(p: Path) -> Path:
    d = state_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{p.stem}_{ts_compact()}{p.suffix}"
    shutil.copy2(p, dst)
    info = p.with_suffix(".info")
    if info.is_file():
        shutil.copy2(info, d / f"{p.stem}_{ts_compact()}.info")
    return dst


def cmd_detect(a, out):
    exe = find_exe()
    dd = data_dir()
    pids = gui_running()
    out["exe"] = str(exe) if exe else None
    out["version"] = exe_version(exe) if exe else None
    out["cli_dialect"] = ("7.3+ (--cli --need-gcode-file)" if exe and is_73_plus(exe) else "7.2") if exe else None
    out["data_dir"] = str(dd) if dd else None
    roots = preset_roots()
    out["preset_dirs"] = {k: [str(x) for x in v] for k, v in roots.items()}
    out["bundled_profiles"] = str(bundled_profiles_dir(exe)) if exe and bundled_profiles_dir(exe) else None
    out["gui_running"] = bool(pids)
    out["gui_pids"] = pids
    out["printer_ip"] = discover_printer_ip()
    out["camera_configured"] = camera_enabled()
    out["python"] = sys.version.split()[0]
    out["pillow"] = _pil() is not None
    if not exe:
        out["warnings"].append("CrealityPrint.exe not found; set CREALITY_PRINT_EXE in .env")
    if pids:
        out["warnings"].append("Creality Print GUI is open: CLI slicing is refused until it is closed (or use --allow-gui / --close-gui).")


def cmd_presets(a, out):
    idx = index_presets()
    if a.presets_cmd == "list":
        rows = [v for v in idx.values()
                if (not a.type or v["type"] == a.type) and (not a.filter or a.filter.lower() in v["name"].lower())
                and (a.all or v["source"] == "user" or v.get("instantiation") in ("true", True))]
        rows.sort(key=lambda v: (v["type"], v["source"], v["name"]))
        out["count"] = len(rows)
        out["presets"] = [{k: r[k] for k in ("name", "type", "source", "inherits", "path")} for r in rows]
    elif a.presets_cmd == "show":
        meta = find_preset(a.name, idx, a.type)
        out["preset"] = meta
        data = resolve_preset(meta["name"], idx, a.type) if a.resolved else _read_json(Path(meta["path"]))
        if a.keys:
            want = [normalize_override(k, "0")[0] for k in a.keys]
            data = {k: data.get(k) for k in want}
        out["settings"] = data
    elif a.presets_cmd == "clone":
        meta = find_preset(a.source, idx, a.type)
        new = a.new_name
        if new in idx:
            raise Fail(f"a preset named {new!r} already exists; pick another name (nothing was overwritten)")
        sets = parse_sets(a.set)
        src = _read_json(Path(meta["path"])) or {}
        if meta["source"] == "user":
            j = dict(src)  # copy user preset as-is (keeps its own inherits)
        else:
            j = {"from": "User", "inherits": meta["name"], "is_custom_defined": "0", "version": src.get("version", "")}
            if src.get("setting_id"):
                j["base_id"] = src["setting_id"]
        resolved = resolve_preset(meta["name"], idx, meta["type"])
        for k, v in sets.items():
            j[k] = coerce_value(resolved.get(k), v)
        j["name"] = new
        idkey = {"process": "print_settings_id", "filament": "filament_settings_id", "machine": "printer_settings_id"}[meta["type"]]
        j[idkey] = [new] if meta["type"] == "filament" else new
        j["from"] = "User"
        dd = data_dir()
        if not dd:
            raise Fail("Creality Print data dir not found (set CREALITY_DATA_DIR)")
        target_root = dd / "user" / (a.account or "default")
        dst = target_root / meta["type"] / f"{safe_name(new)}.json"
        if dst.exists():
            raise Fail(f"{dst} already exists (nothing was overwritten)")
        atomic_write(dst, json.dumps(j, indent=4))
        atomic_write(dst.with_suffix(".info"),
                     f"sync_info = create\nuser_id = \nsetting_id = \nbase_id = {j.get('base_id','')}\nupdated_time = {int(time.time())}\n")
        out["created"] = str(dst)
        out["applied"] = sets
        out["note"] = "Restart Creality Print (or re-open the preset list) to see it in the GUI."
    elif a.presets_cmd == "set":
        meta = find_preset(a.name, idx, a.type)
        sets = parse_sets(a.set)
        if not sets:
            raise Fail("nothing to set; pass --set key=value")
        if meta["source"] != "user":
            raise Fail(f"{meta['name']!r} is a {meta['source']} preset; never edited in place. "
                       f"Use: presets clone \"{meta['name']}\" \"My copy\" --set ...")
        p = Path(meta["path"])
        bk = backup_file(p)
        j = _read_json(p) or {}
        resolved = resolve_preset(meta["name"], idx, meta["type"])
        before = {k: resolved.get(k) for k in sets}
        for k, v in sets.items():
            j[k] = coerce_value(resolved.get(k), v)
        atomic_write(p, json.dumps(j, indent=4))
        out.update(path=str(p), backup=str(bk), before=before, after={k: j[k] for k in sets})
        if gui_running():
            out["warnings"].append("GUI is open: it may overwrite this edit when it saves presets. Close and reopen it.")


# ============================================================ 3MF / mesh helpers
def threemf_info(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        info = {"has_project_settings": "Metadata/project_settings.config" in names,
                "plates": sorted({int(m) for n in names for m in re.findall(r"Metadata/plate_(\d+)\.(?:png|json)", n)}) or [1]}
        if info["has_project_settings"]:
            try:
                cfg = json.loads(z.read("Metadata/project_settings.config"))
                info["printer"] = cfg.get("printer_settings_id")
                info["process"] = cfg.get("print_settings_id")
                info["filaments"] = cfg.get("filament_settings_id")
            except ValueError:
                pass
    return info


def patch_3mf_settings(src: Path, dst: Path, overrides: dict) -> None:
    with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in zi.infolist():
            data = zi.read(item.filename)
            if item.filename == "Metadata/project_settings.config":
                cfg = json.loads(data)
                for k, v in overrides.items():
                    cfg[k] = coerce_value(cfg.get(k), v)
                data = json.dumps(cfg, indent=4).encode()
            zo.writestr(item, data)


_VERT = re.compile(rb'<vertex\s+x="([-\d.eE+]+)"\s+y="([-\d.eE+]+)"\s+z="([-\d.eE+]+)"')


def mesh_points(path: Path) -> list[tuple[float, float, float]]:
    suf = path.suffix.lower()
    pts: list = []
    if suf == ".3mf":
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith(".model"):
                    for m in _VERT.finditer(z.read(n)):
                        pts.append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
    elif suf == ".stl":
        raw = path.read_bytes()
        if len(raw) >= 84:
            n = struct.unpack_from("<I", raw, 80)[0]
            if 84 + n * 50 == len(raw):
                for i in range(n):
                    off = 84 + i * 50 + 12
                    v = struct.unpack_from("<9f", raw, off)
                    pts += [v[0:3], v[3:6], v[6:9]]
                return pts
        for m in re.finditer(rb"vertex\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)", raw):
            pts.append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
    else:
        raise Fail("split-check supports .stl and .3mf")
    if not pts:
        raise Fail("no vertices found in mesh")
    return pts


def _hull(pts2):
    pts2 = sorted(set(pts2))
    if len(pts2) < 3:
        return pts2
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    lo, up = [], []
    for p in pts2:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts2):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    return lo[:-1] + up[:-1]


def cmd_split_check(a, out):
    p = Path(a.file)
    if not p.is_file():
        raise Fail(f"file not found: {p}")
    pts = mesh_points(p)
    xs, ys, zs = zip(*pts)
    size = [max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs)]
    bx, by, bz = a.bed, a.bed_y or a.bed, a.max_z
    hull = _hull([(round(x, 2), round(y, 2)) for x, y, _ in pts])
    big, small = max(bx, by), min(bx, by)
    fit_deg, thin = None, None
    for deg in range(0, 180):
        r = math.radians(deg)
        c, s = math.cos(r), math.sin(r)
        rx = [x*c - y*s for x, y in hull]
        ry = [x*s + y*c for x, y in hull]
        w, h = max(rx)-min(rx), max(ry)-min(ry)
        if fit_deg is None and w <= bx and h <= by:
            fit_deg = deg
        if thin is None or h < thin[2]:
            thin = (deg, w, h)  # orientation where the part is thinnest -> cut along its length
    deg, L, W = thin
    fits_flat = (size[0] <= bx and size[1] <= by) or (size[1] <= bx and size[0] <= by)
    fits_rot = fit_deg is not None
    fits_z = size[2] <= bz
    pieces = 1 if (fits_flat or fits_rot) and fits_z else max(math.ceil(L / big), math.ceil(size[2] / bz), 2)
    out.update(file=str(p), vertices=len(pts), size_mm=[round(v, 1) for v in size],
               bed_mm=[bx, by, bz], fits_as_is=fits_flat and fits_z,
               fits_rotated_z=fits_rot and fits_z, fit_rotation_deg=fit_deg,
               thinnest_rotation_deg=deg, length_x_width_at_thinnest_mm=[round(L, 1), round(W, 1)],
               fits_height=fits_z, suggested_pieces=pieces,
               note=("fits" if pieces == 1 else
                     f"cut along the {round(L)} mm length into about {pieces} pieces (+ joints/pins); "
                     "joint overlap may add a piece"))


# ============================================================ slicing
def parse_gcode_stats(path: Path) -> dict:
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(300_000)
        if size > 600_000:
            f.seek(size - 300_000)
        tail = f.read()
    text = (head + b"\n" + tail).decode("utf-8", "replace")
    st: dict = {}
    pats = {
        "estimated_time": r";\s*(?:model printing time|estimated printing time \(normal mode\))\s*[:=]\s*([^;\n]+)",
        "total_estimated_time": r";\s*total estimated time\s*[:=]\s*([^;\n]+)",
        "filament_g": r";\s*(?:total )?filament used \[g\]\s*=\s*([\d.,\s]+)",
        "filament_mm": r";\s*(?:total )?filament used \[mm\]\s*=\s*([\d.,\s]+)",
        "filament_cm3": r";\s*(?:total )?filament used \[cm3\]\s*=\s*([\d.,\s]+)",
        "layer_count": r";\s*(?:total layer number|total layers count)\s*[:=]\s*(\d+)",
        "time_s_cura": r";TIME:(\d+)",
        "filament_m_cura": r";Filament used:\s*([\d.]+)m",
    }
    for k, rx in pats.items():
        m = re.search(rx, text, re.I)
        if m:
            st[k] = m.group(1).strip()
    if "filament_g" in st and "," in st["filament_g"]:
        try:
            st["per_extruder_g"] = [float(x) for x in st["filament_g"].replace(" ", "").split(",") if x]
        except ValueError:
            pass
    for k in ("filament_g", "filament_mm", "filament_cm3"):
        if k in st:
            try:
                st[k] = round(sum(float(x) for x in st[k].replace(" ", "").split(",") if x), 2)
            except ValueError:
                pass
    st["size_mb"] = round(size / 1e6, 2)
    return st


def build_settings_files(work: Path, printer: str, process: str, filaments: list[str], overrides: dict) -> tuple[list, list]:
    idx = index_presets()
    files = []
    for name, t in ((printer, "machine"), (process, "process")):
        j = resolve_preset(name, idx, t)
        if t == "process":
            for k, v in overrides.items():
                j[k] = coerce_value(j.get(k), v)
        j["from"] = "User"
        p = work / f"{t}_{safe_name(j.get('name', name))}.json"
        atomic_write(p, json.dumps(j, indent=4))
        files.append(str(p))
    fils = []
    for fname in filaments:
        j = resolve_preset(fname, idx, "filament")
        j["from"] = "User"
        p = work / f"filament_{safe_name(j.get('name', fname))}.json"
        atomic_write(p, json.dumps(j, indent=4))
        fils.append(str(p))
    return files, fils


def _flag(name: str, style: str) -> str:
    return "--" + (name.replace("_", "-") if style == "dash" else name)


def _run_slicer(exe: Path, argv: list, timeout: int) -> tuple[int, str]:
    cmd = ([sys.executable, str(exe)] if exe.suffix.lower() == ".py" else [str(exe)]) + argv
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -999, "timeout"


def cmd_slice(a, out):
    exe = find_exe()
    if not exe:
        raise Fail("CrealityPrint.exe not found; set CREALITY_PRINT_EXE")
    src = Path(a.input).resolve()
    if not src.is_file():
        raise Fail(f"input not found: {src}")
    pids = gui_running()
    if pids:
        if a.close_gui:
            for pid in pids:
                subprocess.run(["taskkill", "/PID", str(pid)] if IS_WIN else ["kill", str(pid)], capture_output=True)
            time.sleep(3)
            out["warnings"].append(f"closed Creality Print GUI (pids {pids}) because --close-gui was passed")
        elif not a.allow_gui:
            out["status"] = "skipped"
            out["gui_pids"] = pids
            out["errors"].append("Creality Print GUI is open; CLI slicing shares its config. Close it, or re-run with "
                                 "--close-gui (closes it) or --allow-gui (risky).")
            return
    overrides = parse_sets(a.set)
    for k in ("layer_height", "walls", "infill", "brim"):
        v = getattr(a, k, None)
        if v is not None:
            kk, vv = normalize_override(k, str(v))
            overrides[kk] = vv
    if a.supports is not None:
        overrides["enable_support"] = "1" if a.supports == "on" else "0"
    if "brim_width" in overrides and float(overrides["brim_width"] or 0) > 0:
        overrides.setdefault("brim_type", "outer_only")
    outdir = Path(a.outputdir or (Path(env("PR_OUTPUT_DIR") or (state_dir() / "gcode")) / f"{safe_name(src.stem)}_{ts_compact()}"))
    outdir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pr_slice_"))
    model = src
    load_settings: list = []
    load_fils: list = []
    info = threemf_info(src) if src.suffix.lower() == ".3mf" else {"has_project_settings": False}
    use_presets = bool(a.printer or a.process or a.filament) or not info.get("has_project_settings")
    if use_presets:
        printer = a.printer or DEFAULT_PRINTER
        process = a.process or DEFAULT_PROCESS
        fils = a.filament or [DEFAULT_FILAMENT]
        load_settings, load_fils = build_settings_files(work, printer, process, fils, overrides)
        out["presets_used"] = {"printer": printer, "process": process, "filaments": fils}
        if is_73_plus(exe) and src.suffix.lower() == ".3mf":
            out["warnings"].append("7.3 refuses --load-settings with a 3MF; slice may fail (7.2 is fine).")
    elif overrides:
        model = work / src.name
        patch_3mf_settings(src, model, overrides)
        out["presets_used"] = {"from_3mf": {k: info.get(k) for k in ("printer", "process", "filaments")}}
    else:
        out["presets_used"] = {"from_3mf": {k: info.get(k) for k in ("printer", "process", "filaments")}}
    out["overrides"] = overrides
    logf = outdir / "slicer.log"
    styles = [a.flag_style] if a.flag_style != "auto" else ["underscore", "dash"]
    attempts = []
    produced: list = []
    for style in styles:
        for old in outdir.glob("plate_*.gcode"):
            old.unlink()
        argv = []
        if is_73_plus(exe):
            argv += ["--cli", _flag("need_gcode_file", style)]
        argv += [_flag("slice", style), str(a.plate), _flag("outputdir", style), str(outdir), _flag("logfile", style), str(logf)]
        if a.datadir:
            argv += [_flag("datadir", style), a.datadir]
        if load_settings:
            argv += [_flag("load_settings", style), ";".join(load_settings), _flag("load_filaments", style), ";".join(load_fils)]
        if a.arrange:
            argv += [_flag("arrange", style), "1"]
        if a.orient:
            argv += [_flag("orient", style), "1"]
        argv.append(str(model))
        t0 = time.time()
        code, console = _run_slicer(exe, argv, a.timeout)
        produced = sorted(outdir.glob("plate_*.gcode"))
        logtxt = logf.read_text(encoding="utf-8", errors="replace")[-4000:] if logf.is_file() else ""
        invalid = bool(re.search(r"invalid option|unknown option|unrecognized", console + logtxt, re.I))
        attempts.append({"flag_style": style, "exit_code": code, "seconds": round(time.time() - t0, 1),
                         "gcode_files": [p.name for p in produced], "invalid_option_seen": invalid})
        if code == 0 and produced:
            break
        if not (invalid or (not logtxt and code != 0)):
            break  # real slicing failure, not a flag-dialect problem
    out["attempts"] = attempts
    out["command_preview"] = [str(exe)] + argv
    out["outputdir"] = str(outdir)
    out["log"] = str(logf) if logf.is_file() else None
    shutil.rmtree(work, ignore_errors=True) if not a.keep_temp else None
    last = attempts[-1]
    if last["exit_code"] == 0 and produced:
        out["gcode"] = [str(p) for p in produced]
        out["stats"] = {p.name: parse_gcode_stats(p) for p in produced}
        mat = None
        if out.get("presets_used", {}).get("filaments"):
            mat = next((m for m in MATERIAL_LIMITS if m in str(out["presets_used"]["filaments"][0]).upper()), None)
        out["inspect"] = {}
        for p in produced:
            ins = inspect_gcode(p, mat)
            out["inspect"][p.name] = {k: ins.get(k) for k in ("estimated", "filament_g", "cost_usd", "layers", "max_nozzle", "max_bed", "warnings")}
            out["warnings"] += [f"{p.name}: {w}" for w in ins["warnings"]]
    else:
        out["status"] = "error"
        hint = ""
        if last["exit_code"] in (3221225477, -1073741819):
            hint = " (access violation 0xC0000005: known Creality Print CLI crash, issue #574; try another 3MF or settings)"
        elif last["exit_code"] == -999:
            hint = f" (timed out after {a.timeout}s)"
        out["errors"].append(f"slice failed: exit {last['exit_code']}, gcode files: {len(produced)}{hint}")


def cmd_open(a, out):
    exe = find_exe()
    if not exe:
        raise Fail("CrealityPrint.exe not found")
    f = Path(a.file).resolve()
    if not f.is_file():
        raise Fail(f"file not found: {f}")
    cmd = ([sys.executable, str(exe)] if exe.suffix == ".py" else [str(exe)]) + [str(f)]
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if IS_WIN else 0
    p = subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    out.update(opened=str(f), pid=p.pid, gui_was_running=bool(gui_running()))


# ============================================================ minimal websocket client (RFC 6455)
class WS:
    def __init__(self, host: str, port: int, path: str = "/", timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: wsslicer\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("websocket handshake: connection closed")
            buf += chunk
        head, self._rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError("websocket handshake refused: " + head.split(b"\r\n")[0].decode(errors="replace"))
        expect = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if expect not in head:
            raise OSError("websocket handshake: bad accept key")

    def _recvn(self, n: int) -> bytes:
        while len(self._rest) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("websocket closed")
            self._rest += chunk
        d, self._rest = self._rest[:n], self._rest[n:]
        return d

    def send(self, text: str, opcode: int = 1) -> None:
        payload = text.encode() if isinstance(text, str) else text
        hdr = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self, timeout: float) -> str | None:
        self.sock.settimeout(timeout)
        msg = b""
        while True:
            try:
                b1, b2 = self._recvn(2)
            except socket.timeout:
                return None
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recvn(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recvn(8))[0]
            mask = self._recvn(4) if b2 & 0x80 else None
            data = self._recvn(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                raise OSError("websocket closed by peer")
            if op == 9:
                self.send(data, opcode=10)
                continue
            if op == 10:
                continue
            msg += data
            if b1 & 0x80:
                return msg.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except OSError:
            pass
        self.sock.close()


# ============================================================ printer (Creality OS LAN)
def discover_printer_ip() -> str | None:
    if env("PRINTER_IP"):
        return env("PRINTER_IP")
    dd = data_dir()
    if dd and (dd / "deviceInfo.json").is_file():
        j = _read_json(dd / "deviceInfo.json") or {}
        for g in j.get("groups", []) or []:
            for d in g.get("list", []) or []:
                if d.get("address"):
                    return str(d["address"])
    return None


def printer_cfg() -> dict:
    ip = discover_printer_ip()
    if not ip:
        raise Fail("printer IP unknown: set PRINTER_IP in .env (or add the printer to Creality Print's LAN list)")
    return {"ip": ip, "http_port": int(env("PRINTER_HTTP_PORT") or 80), "ws_port": int(env("PRINTER_WS_PORT") or 9999),
            "gcode_root": env("PRINTER_GCODE_ROOT") or "/usr/data/printer_data/gcodes"}


def ws_exchange(cfg: dict, messages: list[dict], wait: float = 3.0, until=None) -> dict:
    seen: dict = {}
    ws = WS(cfg["ip"], cfg["ws_port"], timeout=5)
    try:
        for m in messages:
            ws.send(json.dumps(m))
        end = time.monotonic() + wait
        while time.monotonic() < end:
            raw = ws.recv(max(0.1, end - time.monotonic()))
            if raw is None:
                break
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("ModeCode") == "heart_beat":
                ws.send("ok")
                continue
            if isinstance(d, dict):
                seen.update(d)
                if until and until(seen):
                    break
    finally:
        ws.close()
    return seen


def _f(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _pos(v):
    if isinstance(v, str):
        m = dict(re.findall(r"([XYZE])\s*:\s*(-?[\d.]+)", v))
        return {k: float(x) for k, x in m.items()} or v
    return v


PRINT_STATES = {0: "idle", 1: "printing", 2: "complete", 3: "failed", 4: "cancelled", 5: "paused"}


def normalize_status(d: dict) -> dict:
    """Creality OS ws keys -> stable JSON for agents. Key names come from K1-family firmware; the CR-10 SE
    is expected to share them (unverified on hardware). Unknown keys stay available under raw."""
    st = d.get("state")
    try:
        st_i = int(st)
    except (TypeError, ValueError):
        st_i = None
    err = d.get("err")
    errcode = err.get("errcode") if isinstance(err, dict) else err
    pick = lambda *ks: next((d[k] for k in ks if k in d), None)
    return {
        "received_at": ts_human(),
        "state": PRINT_STATES.get(st_i, st),
        "state_code": st,
        "device_state": d.get("deviceState"),
        "printing": st_i == 1 or str(d.get("deviceState")) == "1",
        "paused": st_i == 5,
        "file": pick("printFileName", "fileName"),
        "progress_pct": _f(pick("printProgress", "dProgress")),
        "layer": pick("layer", "curLayer"),
        "total_layers": pick("TotalLayer", "totalLayer"),
        "elapsed_s": _f(pick("printJobTime", "printTime")),
        "remaining_s": _f(pick("printLeftTime", "leftTime")),
        "nozzle": {"actual": _f(pick("nozzleTemp")), "target": _f(pick("targetNozzleTemp")),
                   "max": _f(pick("maxNozzleTemp"))},
        "bed": {"actual": _f(pick("bedTemp0", "bedTemp")), "target": _f(pick("targetBedTemp0", "targetBedTemp")),
                "max": _f(pick("maxBedTemp"))},
        "chamber": {"actual": _f(pick("boxTemp", "chamberTemp"))},
        "fans_pct": {"part": _f(pick("modelFanPct", "fan")), "aux": _f(pick("auxiliaryFanPct", "fanAuxiliary")),
                     "case": _f(pick("caseFanPct", "fanCase"))},
        "position": _pos(pick("curPosition", "position")),
        "speed_pct": _f(pick("curFeedratePct", "feedratePct")),
        "flow_pct": _f(pick("curFlowratePct", "flowratePct")),
        "error": {"code": errcode, "key": err.get("key") if isinstance(err, dict) else None, "raw": err},
        "has_error": errcode not in (None, 0, "0", "", {}),
        "model": d.get("model"),
        "hostname": d.get("hostname"),
        "firmware": pick("modelVersion", "softVersion"),
        "firmware_version": (re.search(r"sw ver:\s*(V[\d.]+)", str(pick("modelVersion", "softVersion") or "")) or [None, None])[1],
        # idle = nothing running. A real CR-10 SE reports state 4 (last job cancelled) with deviceState 0 when idle.
        "idle": not (st_i == 1 or st_i == 5 or str(d.get("deviceState")) == "1"),
        # CR-10 SE extras (seen on DWIN V1.1.0.28)
        "light_on": (d["lightSw"] == 1) if "lightSw" in d else None,
        "storage_present": (d["tfCard"] == 1) if "tfCard" in d else None,
        "filament_sensor": {"enabled": d.get("materialDetect"), "status": d.get("materialStatus")},
        "used_material_mm": _f(d.get("usedMaterialLength")),
        "realtime_speed_mm_s": _f(d.get("realTimeSpeed")),
        "realtime_flow_mm3_s": _f(d.get("realTimeFlow")),
        "power_loss_recovery": d.get("repoPlrStatus"),
        "print_id": d.get("printId") or None,
        "camera_on_printer": bool(d.get("video") or d.get("video1")) if ("video" in d or "video1" in d) else None,
    }


def _latest_path() -> Path:
    return state_dir() / "printer_latest.json"


def record_status(norm: dict, raw: dict) -> None:
    """Latest-state JSON (agents read it anytime) + rolling daily log (keeps PR_STREAM_LOG_DAYS, default 7)."""
    atomic_write(_latest_path(), json.dumps({**norm, "raw": raw}, indent=2, default=str))
    d = state_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"printer_stream_{now_et():%Y%m%d}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(norm, default=str) + "\n")
    keep = int(env_float("PR_STREAM_LOG_DAYS", 7))
    for old in sorted(d.glob("printer_stream_*.jsonl"))[:-keep or None]:
        try:
            old.unlink()
        except OSError:
            pass


def printer_status(cfg: dict, wait: float = 3.0) -> dict:
    d = ws_exchange(cfg, [{"method": "get", "params": {"ReqPrinterPara": 1}}], wait=wait,
                    until=lambda v: "state" in v and "nozzleTemp" in v)
    if not d:
        raise Fail("printer websocket answered but sent no status")
    norm = normalize_status(d)
    record_status(norm, d)
    norm["raw_keys"] = sorted(d.keys())[:80]
    return norm


def printer_stream(cfg: dict, seconds: float, jsonl: bool, min_interval: float) -> dict:
    """Subscribe to ws 9999 and merge every push into a live state; emit on change."""
    ws = WS(cfg["ip"], cfg["ws_port"], timeout=5)
    merged: dict = {}
    updates = 0
    last_emit = 0.0
    end = time.monotonic() + seconds
    try:
        ws.send(json.dumps({"method": "get", "params": {"ReqPrinterPara": 1}}))
        while time.monotonic() < end:
            raw = ws.recv(max(0.1, min(2.0, end - time.monotonic())))
            if raw is None:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("ModeCode") == "heart_beat":
                ws.send("ok")
                continue
            if not isinstance(d, dict):
                continue
            merged.update(d)
            updates += 1
            if time.monotonic() - last_emit >= min_interval:
                last_emit = time.monotonic()
                norm = normalize_status(merged)
                record_status(norm, merged)
                if jsonl:
                    sys.stdout.write(json.dumps({"type": "update", **norm}, default=str) + "\n")
                    sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        ws.close()
    if merged:
        record_status(normalize_status(merged), merged)
    return {"updates": updates, "latest": normalize_status(merged) if merged else None,
            "latest_file": str(_latest_path())}


def _tcp_open(ip: str, port: int, t: float | None = None) -> bool:
    t = t or env_float("PR_PROBE_TIMEOUT_S", 1.5)
    try:
        with socket.create_connection((ip, port), timeout=t):
            return True
    except OSError:
        return False


def cmd_printer(a, out):
    sub = a.printer_cmd
    if sub == "discover":
        out["printer_ip"] = discover_printer_ip()
        out["source"] = "PRINTER_IP" if env("PRINTER_IP") else ("Creality Print deviceInfo.json" if out["printer_ip"] else None)
        if not out["printer_ip"]:
            out["warnings"].append("no IP found; set PRINTER_IP in .env")
        return
    if sub in GATED_ACTIONS:
        return request_gated(sub, a, out)
    if sub in PRINTER_QUERIES or sub in ("info", "light", "gcode"):
        return cmd_printer_extra(a, out)
    cfg = printer_cfg()
    out["printer_ip"] = cfg["ip"]
    if sub == "probe":
        plist = [int(x) for x in (env("PR_PROBE_PORTS") or "").split(",") if x.strip().isdigit()] or \
            [cfg["http_port"], cfg["ws_port"], 7125, 4408, 4409, 8080]
        ports = {p: _tcp_open(cfg["ip"], p) for p in plist}
        out["ports_open"] = {str(k): v for k, v in ports.items()}
        if ports.get(cfg["http_port"]):
            try:
                with urllib.request.urlopen(f"http://{cfg['ip']}:{cfg['http_port']}/", timeout=4) as r:
                    out["http_root"] = {"status": r.status, "server": r.headers.get("Server")}
            except urllib.error.HTTPError as e:
                out["http_root"] = {"status": e.code}
            except Exception as e:
                out["http_root"] = {"error": str(e)[:200]}
        if ports.get(cfg["ws_port"]):
            try:
                out["ws_9999"] = printer_status(cfg)
            except Exception as e:
                out["ws_9999"] = {"error": str(e)[:200]}
        if ports.get(7125):
            try:
                with urllib.request.urlopen(f"http://{cfg['ip']}:7125/server/info", timeout=4) as r:
                    out["moonraker"] = json.loads(r.read()).get("result", {}).get("klippy_state")
            except Exception as e:
                out["moonraker"] = {"error": str(e)[:200]}
        out["can_upload"] = bool(ports.get(cfg["http_port"]))
        out["can_control"] = bool(ports.get(cfg["ws_port"]))
        if not any(ports.values()):
            out["status"] = "error"
            out["errors"].append(f"printer not reachable at {cfg['ip']}: every port timed out (off, asleep, "
                                 "other network, or IP changed)")
        elif not ports.get(cfg["ws_port"]):
            out["warnings"].append("port 9999 closed: no live data or control on this unit/firmware")
    elif sub == "status":
        out["printer"] = printer_status(cfg)
        out["latest_file"] = str(_latest_path())
    elif sub == "stream":
        if a.jsonl:
            out["type"] = "summary"
        out.update(printer_stream(cfg, a.seconds, a.jsonl, a.min_interval))
        if not out.get("latest"):
            out["status"] = "error"
            out["errors"].append("no data received from the printer websocket")
    elif sub == "latest":
        p = _latest_path()
        if not p.is_file():
            raise Fail("no saved printer state yet; run `printer status` or `printer stream`")
        j = _read_json(p) or {}
        out["printer"] = {k: v for k, v in j.items() if k != "raw"}
        out["age_s"] = round(time.time() - p.stat().st_mtime, 1)
    elif sub == "upload":
        f = Path(a.file)
        if not f.is_file() or f.suffix.lower() != ".gcode":
            raise Fail("upload needs an existing .gcode file")
        name = safe_name(a.name or f.name)
        if not name.lower().endswith(".gcode"):
            name += ".gcode"
        out.update(upload_gcode(cfg, f, name))
        try:
            names = [str(x.get("name") or x.get("path") or "") for x in printer_files(cfg) if isinstance(x, dict)]
            out["verified_on_printer"] = any(n.endswith(name) for n in names) if names else None
        except Exception:
            out["verified_on_printer"] = None


def upload_gcode(cfg: dict, f: Path, name: str) -> dict:
    boundary = "----pr" + uuid.uuid4().hex
    body = io.BytesIO()
    body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
               f"Content-Type: application/octet-stream\r\n\r\n".encode())
    body.write(f.read_bytes())
    body.write(f"\r\n--{boundary}--\r\n".encode())
    url = f"http://{cfg['ip']}:{cfg['http_port']}/upload/{urllib.parse.quote(name)}"
    req = urllib.request.Request(url, data=body.getvalue(), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            txt = r.read(2000).decode("utf-8", "replace")
            return {"uploaded": name, "http_status": r.status, "printer_reply": txt[:500],
                    "path_on_printer": f"{cfg['gcode_root'].rstrip('/')}/{name}", "mb": round(f.stat().st_size / 1e6, 2)}
    except urllib.error.HTTPError as e:
        raise Fail(f"upload failed: HTTP {e.code} at {url}")
    except OSError as e:
        raise Fail(f"upload failed: {e}")


# ============================================================ owner approval gate
# Physical actions need the OWNER's own "yes" in chat. Flow:
#   `printer start ...` -> pending request, status "awaiting_owner" (summary: part, time, filament, snapshot)
#   owner answers yes/no in chat -> agent runs `approve <request_id> --yes|--no` passing along ONLY the owner's words
#   on yes -> automatic preflight (printer online, idle, no errors, temps sane, file on printer, filament, bed
#   camera check, kill switch) -> executes only if preflight passes; otherwise reports what failed.
def _pending_dir() -> Path:
    d = state_dir() / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _answered_dir() -> Path:
    d = state_dir() / "approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_facts(params: dict) -> dict:
    """Print time / filament grams for the summary, from the local gcode if we know it."""
    lg = params.get("local_gcode")
    if lg and Path(lg).is_file():
        try:
            ins = inspect_gcode(Path(lg))
            return {k: ins.get(k) for k in ("est_time", "est_time_s", "filament_g", "material", "cost_usd") if ins.get(k) is not None}
        except Exception:
            return {}
    return {}


def request_gated(action: str, a, out, reason: str = "", extra: dict | None = None) -> None:
    if not env_bool("PHYSICAL_ACTIONS_ENABLED", True):
        raise Fail("physical actions are switched off (PHYSICAL_ACTIONS_ENABLED=false)")
    params: dict = {}
    if action == "start":
        params["file"] = safe_name(a.file_on_printer)
        params["gcode_root"] = (getattr(a, "gcode_root", None) or env("PRINTER_GCODE_ROOT") or "/usr/data/printer_data/gcodes")
        for k in ("color", "material"):
            if getattr(a, k, None):
                params[k] = getattr(a, k)
    if action == "heat":
        if a.nozzle is None and a.bed is None:
            raise Fail("heat needs --nozzle and/or --bed (0 turns it off)")
        if a.nozzle is not None and not (0 <= a.nozzle <= 260):
            raise Fail("nozzle target must be 0-260 C")
        if a.bed is not None and not (0 <= a.bed <= 100):
            raise Fail("bed target must be 0-100 C")
        params.update(nozzle=a.nozzle, bed=a.bed)
    if action == "fan":
        pct = getattr(a, "pct", None)
        if pct is None or not (0 <= pct <= 100):
            raise Fail("fan needs --pct 0-100")
        params["pct"] = pct
    if action == "gcode":
        params["cmd"] = a.gcode_line.strip()
        if "\n" in params["cmd"] or len(params["cmd"]) > 120:
            raise Fail("one gcode line at a time (max 120 chars)")
    if action == "move":
        params.update(move_params(a))
    if extra and extra.get("_params"):
        params.update(extra.pop("_params"))
    rid = f"r{ts_compact()}_{secrets.token_hex(2)}"
    ttl = env_float("APPROVAL_TTL_MIN", env_float("CONFIRM_TTL_MIN", 15))
    t = now_et()
    facts = _job_facts(params) if action == "start" else {}
    summary = f"Printer {action}"
    if action == "start":
        summary += f": {params['file']}"
        if facts.get("est_time"):
            summary += f", about {facts['est_time']}"
        fil = " ".join(x for x in (params.get("color"), params.get("material") or facts.get("material")) if x)
        if fil or facts.get("filament_g"):
            summary += f", filament {fil or '?'}" + (f" ~{facts['filament_g']} g" if facts.get("filament_g") else "")
        if params.get("swap_note"):
            summary += f". FILAMENT SWAP: {params['swap_note']}"
    if action == "heat":
        summary += f" (nozzle {params['nozzle']}, bed {params['bed']})"
    if action == "move":
        summary += f" ({params['desc']})"
    if action == "gcode":
        summary += f" ({params['cmd']})"
    if action == "fan":
        summary += f" ({params['pct']}%)"
    attach: dict = dict(extra or {})
    photo = None
    if action in ("start", "resume") and camera_enabled():
        try:
            snap = take_snapshot(f"pre_{action}")
            attach["snapshot"] = snap
            photo = snap.get("path")
        except Exception as e:
            attach["snapshot_error"] = str(e)[:200]
    if reason:
        summary = f"{reason}. " + summary
    summary += f". Reply YES or NO (expires in {int(ttl)} min, {ts_human(t)})."
    req = {"request_id": rid, "action": action, "params": params, "facts": facts, "summary": summary,
           "created_at": t.isoformat(timespec="seconds"), "expires_epoch": time.time() + ttl * 60,
           "job_id": out["job_id"], "reason": reason, "attachments": attach, "answer": None}
    atomic_write(_pending_dir() / f"{rid}.json", json.dumps(req, indent=2, default=str))
    pkt = write_packet(f"approve {action}", "approval_request", summary,
                       {"request_id": rid, "action": action, "params": params, "facts": facts,
                        "answer_with": f"approve {rid} --yes | --no", **attach},
                       "awaiting_owner", priority="high")
    out.update(status="awaiting_owner", request_id=rid, action=action, params=params, facts=facts, packet=str(pkt),
               expires_in_min=ttl, summary=summary,
               next=f"show the owner the summary; only on the owner's own yes/no run `approve {rid} --yes` or `--no`",
               **attach)
    if env_bool("APPROVAL_TELEGRAM", False):
        out["telegram"] = telegram_send(summary, photo)


def _check(checks: list, name: str, ok, detail: str = "", blocking: bool = True) -> None:
    checks.append({"check": name, "ok": ok, "blocking": blocking, "detail": detail})


def preflight(action: str, params: dict) -> dict:
    """Automated go/no-go before a physical action. Read-only (one camera snapshot at most)."""
    checks: list = []
    _check(checks, "physical_actions_enabled", env_bool("PHYSICAL_ACTIONS_ENABLED", True),
           "PHYSICAL_ACTIONS_ENABLED must be true")
    cfg = printer_cfg()
    st = None
    try:
        st = printer_status(cfg)
        _check(checks, "printer_online", True, f"{cfg['ip']} answered: state {st.get('state')}")
    except Exception as e:
        _check(checks, "printer_online", False, f"{cfg.get('ip')}: {str(e)[:160]}")
    if st:
        printing, paused = bool(st.get("printing")), bool(st.get("paused"))
        if action in ("start", "home"):
            _check(checks, "printer_idle", bool(st.get("idle", not printing and not paused)) and not printing and not paused,
                   f"state {st.get('state')}, deviceState {st.get('device_state')}")
        elif action == "move":
            _check(checks, "printer_idle_or_paused", (not printing) or paused, f"state {st.get('state')}")
        elif action == "pause":
            _check(checks, "printer_printing", printing and not paused, f"state {st.get('state')}")
        elif action == "resume":
            _check(checks, "printer_paused", paused, f"state {st.get('state')}")
        elif action == "cancel":
            _check(checks, "job_running", printing or paused, f"state {st.get('state')}")
        _check(checks, "no_printer_error", not st.get("has_error"), f"error {st.get('error', {}).get('raw')}",
               blocking=action not in ("pause", "cancel"))
        nz, bd = st.get("nozzle") or {}, st.get("bed") or {}
        sane = True
        notes = []
        for nm, t, lim in (("nozzle", nz, 275), ("bed", bd, 115)):
            act, tgt = t.get("actual"), t.get("target")
            if act is None:
                notes.append(f"{nm} reading missing")
                sane = False
            elif act > lim or act < -5:
                notes.append(f"{nm} {act} C out of range")
                sane = False
            elif tgt and act > tgt + env_float("WATCH_TEMP_RUNAWAY_C", 15):
                notes.append(f"{nm} {act} C above target {tgt}")
                sane = False
            else:
                notes.append(f"{nm} {act}/{tgt} C")
        _check(checks, "temps_sane", sane, "; ".join(notes), blocking=action not in ("pause", "cancel"))
    if action == "start":
        try:
            names = [str(f.get("name") or f.get("fileName") or f.get("filename") or "") if isinstance(f, dict) else str(f)
                     for f in printer_files(cfg)]
        except Exception as e:
            names = None
            err = str(e)[:120]
        if names:
            hit = any(n.rsplit("/", 1)[-1] == params["file"] for n in names)
            _check(checks, "file_on_printer", hit, params["file"] + (" found" if hit else f" not in {len(names)} printer files"))
        else:
            _check(checks, "file_on_printer", None, "printer did not list files; could not verify " + params["file"]
                   + (f" ({err})" if names is None else ""), blocking=False)
        facts = _job_facts(params)
        inv = inventory_check(params.get("material") or facts.get("material"), params.get("color"), facts.get("filament_g"))
        _check(checks, "filament_on_hand", inv.get("ok"), inv.get("note") or f"spool {inv.get('spool', {}).get('id')}",
               blocking=inv.get("ok") is not None)
    if action in ("start", "resume") and camera_enabled():
        try:
            snap = take_snapshot(f"preflight_{action}")
            bc = bed_check(snap.get("raw_path")) if action == "start" else {"verdict": "snapshot_only"}
            score, thr = bc.get("diff_score"), bc.get("threshold")
            bad = score is not None and thr and score >= thr * env_float("BEDCHECK_BLOCK_FACTOR", 2.0)
            ok = True if bc["verdict"] in ("looks_clear", "snapshot_only") else (False if bad else None)
            _check(checks, "bed_clear_camera", ok, f"{bc['verdict']} (score {score}, threshold {thr}); photo {snap.get('path')}",
                   blocking=bool(bad))
        except Exception as e:
            _check(checks, "bed_clear_camera", None, f"camera failed: {str(e)[:120]}; look in person", blocking=False)
    failed = [c for c in checks if c["blocking"] and c["ok"] is False]
    flagged = [c for c in checks if not c["blocking"] and c["ok"] is not True]
    return {"passed": not failed, "failed": [c["check"] for c in failed], "flagged": [c["check"] for c in flagged],
            "checks": checks, "checked_at": ts_human()}


def _find_request(rid: str) -> tuple[dict, Path]:
    rid = safe_name(rid.strip())
    p = _pending_dir() / f"{rid}.json"
    if not p.is_file():
        done = _answered_dir() / f"{rid}.json"
        raise Fail(f"request {rid} already answered" if done.is_file() else f"unknown request {rid}")
    return _read_json(p) or {}, p


def cmd_approve(a, out):
    req, p = _find_request(a.request_id)
    answer = "yes" if a.yes else "no"
    req["answer"] = {"answer": answer, "approved_by": a.by or "owner_chat", "owner_words": a.quote,
                     "answered_at": ts_human()}
    out.update(request_id=req["request_id"], action=req["action"], params=req["params"], answer=req["answer"])
    expired = time.time() > req.get("expires_epoch", 0)
    os.replace(p, _answered_dir() / p.name)  # single use
    dst = _answered_dir() / p.name
    if req["params"].get("queue_id") and (answer == "no" or expired):
        _queue_set(req["params"]["queue_id"], status="queued", request_id=None)
    if answer == "no":
        out["status"] = "declined"
        req["outcome"] = "declined"
        atomic_write(dst, json.dumps(req, indent=2, default=str))
        write_packet(f"{req['action']} declined", "result", f"Owner said no to printer {req['action']}.", req["answer"], "declined")
        return
    if expired:
        req["outcome"] = "expired"
        atomic_write(dst, json.dumps(req, indent=2, default=str))
        raise Fail("request expired; ask again")
    pf = preflight(req["action"], req["params"])
    out["preflight"] = pf
    req["preflight"] = pf
    if not pf["passed"]:
        req["outcome"] = "preflight_failed"
        if req["params"].get("queue_id"):
            _queue_set(req["params"]["queue_id"], status="queued", request_id=None)
        atomic_write(dst, json.dumps(req, indent=2, default=str))
        out["status"] = "preflight_failed"
        msg = f"Owner approved printer {req['action']}, but preflight failed: {', '.join(pf['failed'])}. Nothing was sent."
        out["errors"].append(msg)
        write_packet(f"{req['action']} preflight failed", "result", msg, {"preflight": pf}, "preflight_failed", priority="high")
        return
    out["result"] = execute_physical(req["action"], req["params"])
    req["outcome"] = "executed"
    req["result"] = out["result"]
    atomic_write(dst, json.dumps(req, indent=2, default=str))
    if pf["flagged"]:
        out["warnings"].append("preflight flags (not blocking): " + ", ".join(pf["flagged"]))
    write_packet(f"{req['action']} done", "result",
                 f"Printer {req['action']} executed after owner yes ({req['answer']['approved_by']}) and preflight pass.",
                 {"action": req["action"], "params": req["params"], "result": out["result"], "preflight": pf}, "ok")


def cmd_preflight(a, out):
    req, _ = _find_request(a.request_id)
    out.update(request_id=req["request_id"], action=req["action"], preflight=preflight(req["action"], req["params"]))


def move_params(a) -> dict:
    axes = {k: getattr(a, k) for k in ("x", "y", "z") if getattr(a, k, None) is not None}
    if not axes:
        raise Fail("move needs --x/--y/--z")
    feed = int(getattr(a, "feed", None) or 3000)
    if not (100 <= feed <= 6000):
        raise Fail("feed must be 100-6000 mm/min")
    bx, by, bz = float(env("BED_X_MM") or 220), float(env("BED_Y_MM") or 220), float(env("MAX_Z_MM") or 250)
    if a.relative:
        if any(abs(v) > 50 for v in axes.values()):
            raise Fail("relative moves are limited to 50 mm per axis")
    else:
        lim = {"x": bx, "y": by, "z": bz}
        if any(not (0 <= v <= lim[k]) for k, v in axes.items()):
            raise Fail(f"absolute move outside 0..{bx:.0f} x 0..{by:.0f} x 0..{bz:.0f}")
    g1 = "G1 " + " ".join(f"{k.upper()}{v:g}" for k, v in axes.items()) + f" F{feed}"
    return {"relative": bool(a.relative), "gcode": (["G91", g1, "G90"] if a.relative else ["G90", g1]),
            "desc": ("relative " if a.relative else "to ") + g1[3:]}


def execute_physical(action: str, params: dict) -> dict:
    if not env_bool("PHYSICAL_ACTIONS_ENABLED", True):  # kill switch also covers auto-pause and every caller
        raise Fail("physical actions are switched off (PHYSICAL_ACTIONS_ENABLED=false)")
    cfg = printer_cfg()
    if action == "start":
        st = printer_status(cfg)
        if st.get("printing"):
            raise Fail("printer is already printing; not starting another job")
        path = f"{params['gcode_root'].rstrip('/')}/{params['file']}"
        reply = ws_exchange(cfg, [{"method": "set", "params": {"opGcodeFile": f"printprt:{path}"}}], wait=4)
        _watch_reset()
        rec = on_job_started({**params, "file": params["file"]})
        return {"history_id": rec["id"], "sent": "opGcodeFile", "path": path,
                "printer_reply": {k: reply.get(k) for k in ("err", "state", "printFileName") if k in reply}}
    if action in ("pause", "resume", "cancel"):
        p = {"pause": {"pause": 1}, "resume": {"pause": 0}, "cancel": {"stop": 1}}[action]
        reply = ws_exchange(cfg, [{"method": "set", "params": p}], wait=2)
        return {"sent": p, "printer_reply": {k: reply.get(k) for k in ("err", "state") if k in reply}}
    if action == "heat":
        msgs = []
        if params.get("nozzle") is not None:
            msgs.append({"method": "set", "params": {"gcodeCmd": f"M104 S{int(params['nozzle'])}"}})
        if params.get("bed") is not None:
            msgs.append({"method": "set", "params": {"gcodeCmd": f"M140 S{int(params['bed'])}"}})
        reply = ws_exchange(cfg, msgs, wait=2)
        return {"sent": [m["params"] for m in msgs], "printer_reply": {k: reply.get(k) for k in ("err",) if k in reply}}
    if action == "move":
        st = printer_status(cfg)
        if st.get("printing") and not st.get("paused"):
            raise Fail("printer is printing; not moving the head")
        msgs = [{"method": "set", "params": {"gcodeCmd": g}} for g in params["gcode"]]
        reply = ws_exchange(cfg, msgs, wait=2)
        return {"sent": params["gcode"], "printer_reply": {k: reply.get(k) for k in ("err",) if k in reply}}
    if action in ("gcode", "home", "fan"):
        cmd = {"home": "G28", "fan": f"M106 S{int(params.get('pct', 0) * 255 / 100 + 0.5)}"}.get(action) or params["cmd"]
        reply = ws_exchange(cfg, [{"method": "set", "params": {"gcodeCmd": cmd}}], wait=2)
        return {"sent": {"gcodeCmd": cmd}, "printer_reply": {k: reply.get(k) for k in ("err", "state") if k in reply}}
    raise Fail(f"unknown action {action}")


def cmd_pending(a, out):
    rows = []
    for p in sorted(_pending_dir().glob("*.json")):
        r = _read_json(p) or {}
        rows.append({"request_id": r.get("request_id"), "action": r.get("action"), "summary": r.get("summary"),
                     "created_at": r.get("created_at"), "expired": time.time() > r.get("expires_epoch", 0)})
    out["pending"] = rows


# ============================================================ camera
def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa
        return Image
    except Exception:
        return None


def camera_enabled() -> bool:
    return bool(env("CAMERA_URL")) and env_bool("CAMERA_ENABLED", True)


def _fetch_frame(url: str) -> bytes:
    if url.startswith("file://"):
        return Path(urllib.request.url2pathname(urllib.parse.urlparse(url).path)).read_bytes()
    if os.path.isfile(url):
        return Path(url).read_bytes()
    with urllib.request.urlopen(url, timeout=float(env("CAMERA_TIMEOUT_S") or 8)) as r:
        ctype = r.headers.get("Content-Type", "")
        if "multipart" in ctype:  # MJPEG stream: grab the first full JPEG
            buf = b""
            while len(buf) < 8_000_000:
                chunk = r.read(65536)
                if not chunk:
                    break
                buf += chunk
                s = buf.find(b"\xff\xd8")
                e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                if s >= 0 and e > 0:
                    return buf[s:e + 2]
            raise Fail("no JPEG frame found in camera stream")
        return r.read(15_000_000)


def take_snapshot(label: str = "snap") -> dict:
    if not env("CAMERA_URL"):
        raise Fail("CAMERA_URL not set")
    t = now_et()
    raw = _fetch_frame(env("CAMERA_URL"))
    d = state_dir() / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{safe_name(label)}_{ts_compact(t)}_ET"
    raw_path = d / "raw" / f"{stem}.jpg"
    n = 2
    while raw_path.exists() or (d / f"{stem}.jpg").exists():  # same second: keep every frame
        stem = f"{safe_name(label)}_{ts_compact(t)}_ET_{n}"
        raw_path = d / "raw" / f"{stem}.jpg"
        n += 1
    atomic_write(raw_path, raw)
    out = {"taken_at": t.isoformat(timespec="seconds"), "taken_at_human": ts_human(t), "raw_path": str(raw_path),
           "path": str(raw_path), "timestamp_burned_in": False}
    Image = _pil()
    if Image:
        from PIL import ImageDraw, ImageFont
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            dr = ImageDraw.Draw(im)
            size = max(14, im.width // 40)
            try:
                font = ImageFont.truetype("arial.ttf", size) if IS_WIN else ImageFont.truetype("DejaVuSans.ttf", size)
            except Exception:
                font = ImageFont.load_default()
            text = f"{ts_human(t)}  {label}"
            bb = dr.textbbox((0, 0), text, font=font)
            pad = 6
            dr.rectangle([0, im.height - (bb[3]-bb[1]) - 3*pad, bb[2]-bb[0] + 2*pad, im.height], fill=(0, 0, 0))
            dr.text((pad, im.height - (bb[3]-bb[1]) - 2*pad), text, fill=(255, 255, 0), font=font)
            p = d / f"{stem}.jpg"
            b = io.BytesIO()
            im.save(b, "JPEG", quality=88)
            atomic_write(p, b.getvalue())
            out.update(path=str(p), timestamp_burned_in=True, size_px=[im.width, im.height])
        except Exception as e:
            out["burn_in_error"] = str(e)[:200]
    else:
        out["note"] = "Pillow not installed: timestamp only in filename/packet"
    return out


def image_diff(a_path: str, b_path: str) -> float | None:
    """Mean absolute grayscale difference on a 160x120 thumbnail, 0-100. None without Pillow."""
    Image = _pil()
    if not Image:
        return None
    from PIL import ImageFilter
    def load(p):
        return Image.open(p).convert("L").resize((160, 120)).filter(ImageFilter.GaussianBlur(1))
    x, y = load(a_path), load(b_path)
    try:
        import numpy as np
        return round(float(np.abs(np.asarray(x, dtype=np.int16) - np.asarray(y, dtype=np.int16)).mean()) / 2.55, 2)
    except ImportError:
        px, py = list(x.getdata()), list(y.getdata())
        return round(sum(abs(i - j) for i, j in zip(px, py)) / len(px) / 2.55, 2)


def reference_path() -> Path:
    return state_dir() / "camera" / "empty_bed_reference.jpg"


def bed_check(raw_path: str | None) -> dict:
    ref = reference_path()
    thr = env_float("BED_CLEAR_DIFF_THRESHOLD", 6.0)
    if not raw_path:
        return {"verdict": "no_snapshot", "auto_approve": False}
    if not ref.is_file():
        return {"verdict": "no_reference", "auto_approve": False,
                "note": "run `camera set-reference` with an empty bed to enable the automatic check"}
    score = image_diff(str(ref), raw_path)
    if score is None:
        return {"verdict": "unavailable_no_pillow", "auto_approve": False}
    verdict = "looks_clear" if score <= thr else "possibly_not_clear"
    return {"verdict": verdict, "diff_score": score, "threshold": thr, "reference": str(ref), "auto_approve": False,
            "note": "heuristic only: lighting, camera bumps and bed position change the score; a human still checks"}


def cmd_camera(a, out):
    if a.camera_cmd == "set-reference":
        if a.from_file:
            src = Path(a.from_file)
            if not src.is_file():
                raise Fail(f"not found: {src}")
            raw = src.read_bytes()
        else:
            snap = take_snapshot("empty_bed_reference")
            raw = Path(snap["raw_path"]).read_bytes()
            out["snapshot"] = snap
        ref = reference_path()
        if ref.exists():
            backup_file(ref)
        atomic_write(ref, raw)
        out["reference"] = str(ref)
        out["set_at"] = ts_human()
        return
    snap = take_snapshot(a.label if a.camera_cmd == "snapshot" else "bed_check")
    out["snapshot"] = snap
    if a.camera_cmd == "bed-check":
        out["bed_check"] = bed_check(snap["raw_path"])
        out["summary"] = "Check the bed is clear (automatic check is advisory only)."


# ============================================================ malfunction watch
def _watch_file() -> Path:
    return state_dir() / "watch_state.json"


def _watch_reset() -> None:
    try:
        _watch_file().unlink()
    except OSError:
        pass


def telegram_send(text: str, photo: str | None = None) -> dict:
    tok, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not (tok and chat) or not env_bool("TELEGRAM_ALERTS", True):
        return {"sent": False, "reason": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set or TELEGRAM_ALERTS=false"}
    base = (env("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/")
    try:
        if photo and Path(photo).is_file():
            boundary = "----pr" + uuid.uuid4().hex
            b = io.BytesIO()
            for k, v in (("chat_id", chat), ("caption", text[:1000])):
                b.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
            b.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"snap.jpg\"\r\n"
                    f"Content-Type: image/jpeg\r\n\r\n".encode())
            b.write(Path(photo).read_bytes())
            b.write(f"\r\n--{boundary}--\r\n".encode())
            req = urllib.request.Request(f"{base}/bot{tok}/sendPhoto", data=b.getvalue(),
                                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        else:
            req = urllib.request.Request(f"{base}/bot{tok}/sendMessage",
                                         data=urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode())
        with urllib.request.urlopen(req, timeout=20) as r:
            return {"sent": r.status == 200, "http_status": r.status}
    except Exception as e:
        return {"sent": False, "error": re.sub(r"bot[^/\s]+", "bot***", str(e))[:200]}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def watch_tick(out: dict) -> dict:
    """One observation. Never acts on its own except AUTO_PAUSE_ON_ANOMALY=true (default false)."""
    ws_state = _read_json(_watch_file()) or {}
    now = time.time()
    anomalies = []
    status, snap = None, None
    try:
        status = printer_status(printer_cfg(), wait=float(env("WATCH_WS_WAIT_S") or 3))
    except Exception as e:
        out["warnings"].append(f"printer status unavailable: {str(e)[:150]}")
    if camera_enabled():
        try:
            snap = take_snapshot("watch")
        except Exception as e:
            out["warnings"].append(f"snapshot failed: {str(e)[:150]}")
    printing = bool(status and status.get("printing"))
    if status:
        if status.get("has_error"):
            anomalies.append({"kind": "printer_error", "detail": f"printer reports error {status['error'].get('raw')}"})
        runaway = env_float("WATCH_TEMP_RUNAWAY_C", 15)
        drop = env_float("WATCH_TEMP_DROP_C", 15)
        for part, warm_s, hot in (("nozzle", 300, 100), ("bed", 600, 40)):
            act, tgt = status[part].get("actual"), status[part].get("target")
            if act is None:
                continue
            if tgt and act > tgt + runaway:
                anomalies.append({"kind": f"{part}_temp_runaway", "detail": f"{part} {act} C is above target {tgt} C"})
            elif not tgt and act > env_float(f"WATCH_{part.upper()}_IDLE_MAX_C", 70 if part == "nozzle" else 60) \
                    and not printing:
                prev = (ws_state.get("last_temps") or {}).get(part)
                if prev is not None and act > prev + 2:
                    anomalies.append({"kind": f"{part}_heating_without_target",
                                      "detail": f"{part} rising ({prev} -> {act} C) with no target set"})
            if printing and tgt and tgt > hot and act < tgt - drop and ws_state.get("printing_since") and \
                    now - ws_state["printing_since"] > warm_s:
                anomalies.append({"kind": f"{part}_temp_drop", "detail": f"{part} {act} C vs target {tgt} C"})
        ws_state["last_temps"] = {"nozzle": status["nozzle"].get("actual"), "bed": status["bed"].get("actual")}
    if printing and not ws_state.get("printing_since"):
        ws_state["printing_since"] = now
    if not printing:
        ws_state.pop("printing_since", None)
    diff_prev = None
    if snap and ws_state.get("last_raw") and Path(ws_state["last_raw"]).is_file():
        diff_prev = image_diff(ws_state["last_raw"], snap["raw_path"])
    if diff_prev is not None:
        if diff_prev >= env_float("WATCH_DIFF_SPIKE", 20):
            anomalies.append({"kind": "big_frame_change",
                              "detail": f"frame changed {diff_prev}% since last check (spaghetti, part knocked loose, camera bumped or lights changed?)"})
        if diff_prev < env_float("WATCH_STALL_DIFF", 0.4):
            ws_state.setdefault("still_since", ws_state.get("last_ts", now))
        else:
            ws_state.pop("still_since", None)
    prog = status.get("progress_pct") if status else None
    if prog is not None:
        if ws_state.get("last_progress") == prog:
            ws_state.setdefault("progress_still_since", ws_state.get("last_ts", now))
        else:
            ws_state.pop("progress_still_since", None)
        ws_state["last_progress"] = prog
    stall_s = env_float("WATCH_STALL_MINUTES", 12) * 60
    if printing:
        if ws_state.get("still_since") and now - ws_state["still_since"] >= stall_s:
            anomalies.append({"kind": "possible_stall_camera",
                              "detail": f"no visible change for {int((now - ws_state['still_since'])/60)} min while printing"})
        if ws_state.get("progress_still_since") and now - ws_state["progress_still_since"] >= stall_s:
            anomalies.append({"kind": "possible_stall_progress",
                              "detail": f"progress stuck at {prog}% for {int((now - ws_state['progress_still_since'])/60)} min"})
    if snap:
        ws_state["last_raw"] = snap["raw_path"]
    ws_state["last_ts"] = now
    # alert with cooldown per kind
    cooldown = env_float("ALERT_COOLDOWN_MIN", 15) * 60
    last_alert = ws_state.setdefault("last_alert", {})
    fresh = [x for x in anomalies if now - last_alert.get(x["kind"], 0) >= cooldown]
    result = {"checked_at": ts_human(), "printing": printing, "status": status, "snapshot": snap,
              "frame_diff_vs_previous": diff_prev, "anomalies": anomalies, "alerted": []}
    if fresh:
        for x in fresh:
            last_alert[x["kind"]] = now
        kinds = ", ".join(x["kind"] for x in fresh)
        text = f"Printer alert {ts_human()}: " + "; ".join(x["detail"] for x in fresh)
        action = {}
        if printing and env_bool("AUTO_PAUSE_ON_ANOMALY", False):
            try:
                action = {"auto_paused": True, "result": execute_physical("pause", {})}
                text += " | AUTO-PAUSED (AUTO_PAUSE_ON_ANOMALY=true)"
            except Exception as e:
                action = {"auto_paused": False, "error": str(e)[:200]}
        elif printing:
            sub: dict = {"status": "", "warnings": [], "errors": [], "job_id": out["job_id"]}
            try:
                request_gated("pause", argparse.Namespace(), sub, reason=f"Malfunction watch: {kinds}")
                action = {"pause_requested": True, "request_id": sub.get("request_id")}
                text += f" | Pause? Reply YES or NO (request {sub.get('request_id')})"
            except Fail as e:
                action = {"pause_requested": False, "reason": str(e)}
        pkt = write_packet(f"ALERT {kinds}", "alert", text,
                           {"anomalies": fresh, "snapshot": snap, "status": status, **action}, "alert", priority="high")
        result["alerted"] = fresh
        result["packet"] = str(pkt)
        result["action"] = action
        result["telegram"] = telegram_send(text, snap.get("path") if snap else None)
    if status:
        result["events"] = watch_job_events(status, ws_state, snap, result)
    atomic_write(_watch_file(), json.dumps(ws_state, indent=2))
    return result


def cmd_watch(a, out):
    if a.reset:
        _watch_reset()
    ticks = []
    end = time.time() + a.max_minutes * 60
    while True:
        r = watch_tick(out)
        ticks.append(r)
        if a.once or time.time() >= end or (not r["printing"] and a.stop_when_idle and len(ticks) > 1):
            break
        time.sleep(max(5, a.interval))
    out["ticks"] = ticks if not a.once else None
    out["last"] = ticks[-1]
    if any(t["alerted"] for t in ticks):
        out["status"] = "alert"


# ============================================================ stores / notify / costs
def _store(name: str) -> Path:
    return state_dir() / f"{name}.json"


def load_store(name: str, default):
    j = _read_json(_store(name))
    return j if j is not None else default


def save_store(name: str, data) -> None:
    atomic_write(_store(name), json.dumps(data, indent=2, default=str))


def notify(subject: str, text: str, output: dict, priority: str = "normal", photo: str | None = None,
           ptype: str = "notice") -> dict:
    pkt = write_packet(subject, ptype, text, output, ptype, priority=priority)
    tg = telegram_send(text, photo) if env_bool("NOTIFY_TELEGRAM", True) else {"sent": False, "reason": "NOTIFY_TELEGRAM=false"}
    return {"packet": str(pkt), "telegram": tg}


def cost_per_kg(material: str | None = None, color: str | None = None) -> float:
    for sp in load_store("inventory", []):
        if sp.get("archived"):
            continue
        if material and color and sp.get("material", "").upper() == material.upper() and \
                sp.get("color", "").lower() == color.lower() and sp.get("cost_per_kg"):
            return float(sp["cost_per_kg"])
    return env_float("FILAMENT_COST_PER_KG", 20.0)


def fmt_dur(sec) -> str | None:
    if sec is None:
        return None
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def parse_dur(text: str | None) -> int | None:
    """'1h 2m 3s' / '1d 2h' / '45m 10s' -> seconds."""
    if not text:
        return None
    parts = dict((u, int(v)) for v, u in re.findall(r"(\d+)\s*([dhms])", text))
    if not parts:
        return None
    return parts.get("d", 0) * 86400 + parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0)


# ============================================================ gcode inspector
MATERIAL_LIMITS = {  # nozzle min/max, bed min/max (C): sane ranges, warnings only
    "PETG": (220, 245, 60, 85), "PLA": (190, 225, 45, 65), "ABS": (230, 260, 90, 110),
    "ASA": (235, 265, 90, 110), "TPU": (210, 235, 30, 60), "PLA-CF": (200, 230, 45, 65),
}
_G_MOVE = re.compile(r"^G[01]\s")
_AX = re.compile(r"([XYZE])(-?[\d.]+)")


def inspect_gcode(path: Path, material: str | None = None, bed: tuple[float, float] | None = None) -> dict:
    if not path.is_file():
        raise Fail(f"gcode not found: {path}")
    noz, beds, tools, pauses = [], [], set(), []
    layers = 0
    ftype = None
    absolute = True
    xmin = ymin = 1e9
    xmax = ymax = -1e9
    zmax = 0.0
    x = y = z = 0.0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith(";"):
                if line.startswith((";LAYER_CHANGE", ";LAYER:", "; CHANGE_LAYER")):
                    layers += 1
                elif ftype is None and "filament_type" in line:
                    m = re.search(r"filament_type\s*=\s*([^;\n]+)", line)
                    if m:
                        ftype = m.group(1).split(";")[0].strip()
                continue
            c = line.split(";", 1)[0].strip()
            if not c:
                continue
            w = c.split()[0].upper()
            if w in ("G0", "G1"):
                v = dict(_AX.findall(c))
                if absolute:
                    x = float(v.get("X", x)); y = float(v.get("Y", y)); z = float(v.get("Z", z))
                    if "E" in v and float(v["E"]) > 0 and ("X" in v or "Y" in v):
                        xmin, xmax, ymin, ymax = min(xmin, x), max(xmax, x), min(ymin, y), max(ymax, y)
                        zmax = max(zmax, z)
            elif w == "G90":
                absolute = True
            elif w == "G91":
                absolute = False
            elif w in ("M104", "M109"):
                m = re.search(r"S(\d+(\.\d+)?)", c)
                if m:
                    noz.append(float(m.group(1)))
            elif w in ("M140", "M190"):
                m = re.search(r"S(\d+(\.\d+)?)", c)
                if m:
                    beds.append(float(m.group(1)))
            elif re.fullmatch(r"T\d+", w):
                tools.add(w)
            elif w in ("M600", "M601", "PAUSE", "M0", "M25"):
                pauses.append({"cmd": w, "at_layer": layers})
    st = parse_gcode_stats(path)
    mat = (material or (ftype or "").split(",")[0].strip().strip('"') or None)
    mat = mat.upper() if mat else None
    warnings = []
    hot_noz = [t for t in noz if t > 0]
    hot_bed = [t for t in beds if t > 0]
    if not hot_noz:
        warnings.append("no nozzle heat command (M104/M109) found")
    lim = MATERIAL_LIMITS.get(mat or "")
    if lim and hot_noz and max(hot_noz) > lim[1]:
        warnings.append(f"nozzle {max(hot_noz):.0f} C is above the usual {mat} max {lim[1]} C")
    if lim and hot_noz and min(hot_noz) < lim[0]:
        warnings.append(f"nozzle {min(hot_noz):.0f} C is below the usual {mat} min {lim[0]} C")
    if lim and hot_bed and max(hot_bed) > lim[3]:
        warnings.append(f"bed {max(hot_bed):.0f} C is above the usual {mat} max {lim[3]} C")
    bx, by = bed or (float(env("BED_X_MM") or 220), float(env("BED_Y_MM") or 220))
    if xmax > -1e9 and (xmin < -1 or ymin < -1 or xmax > bx + 1 or ymax > by + 1):
        warnings.append(f"extrusion outside the {bx:.0f}x{by:.0f} bed (X {xmin:.0f}..{xmax:.0f}, Y {ymin:.0f}..{ymax:.0f})")
    if zmax > float(env("MAX_Z_MM") or 250):
        warnings.append(f"print height {zmax:.1f} mm exceeds MAX_Z_MM")
    if pauses:
        warnings.append(f"{len(pauses)} pause / filament-change command(s): someone must be there to swap filament")
    if len(tools) > 1:
        warnings.append(f"multi-tool gcode ({sorted(tools)}) on a single-extruder printer: tool changes act as pauses or fail")
    grams = st.get("filament_g")
    secs = parse_dur(st.get("estimated_time") or st.get("total_estimated_time")) or (int(st["time_s_cura"]) if st.get("time_s_cura") else None)
    out = {"file": str(path), "material": mat, "nozzle_temps": sorted(set(noz)), "bed_temps": sorted(set(beds)),
           "first_nozzle": hot_noz[0] if hot_noz else None, "max_nozzle": max(hot_noz) if hot_noz else None,
           "max_bed": max(hot_bed) if hot_bed else None, "layers": layers or st.get("layer_count"),
           "tools": sorted(tools), "pauses": pauses,
           "extents_mm": None if xmax < -1e8 else {"x": [round(xmin, 1), round(xmax, 1)], "y": [round(ymin, 1), round(ymax, 1)], "z_max": round(zmax, 2)},
           "estimated_s": secs, "estimated": fmt_dur(secs), "filament_g": grams, "filament_mm": st.get("filament_mm"),
           "per_extruder_g": st.get("per_extruder_g"), "size_mb": st.get("size_mb"), "warnings": warnings}
    if grams:
        out["cost_usd"] = round(grams / 1000 * cost_per_kg(mat), 2)
        out["cost_basis_per_kg"] = cost_per_kg(mat)
    return out


def cmd_gcode(a, out):
    out["inspect"] = inspect_gcode(Path(a.file), a.material)
    out["warnings"] += out["inspect"]["warnings"]


# ============================================================ history
def on_job_started(params: dict) -> dict:
    hist = load_store("history", [])
    rec = {"id": f"h{len(hist) + 1:04d}", "file": params.get("file"), "started_at": ts_human(),
           "started_epoch": time.time(), "result": "printing", "queue_id": params.get("queue_id"),
           "local_gcode": params.get("local_gcode"), "color": params.get("color"), "material": params.get("material")}
    if params.get("local_gcode") and Path(params["local_gcode"]).is_file():
        try:
            ins = inspect_gcode(Path(params["local_gcode"]), params.get("material"))
            rec.update(est_s=ins.get("estimated_s"), est_g=ins.get("filament_g"), est_cost=ins.get("cost_usd"))
        except Fail:
            pass
    hist.append(rec)
    save_store("history", hist)
    if params.get("queue_id"):
        _queue_set(params["queue_id"], status="printing", history_id=rec["id"])
    return rec


def open_job() -> dict | None:
    return next((h for h in reversed(load_store("history", [])) if h.get("result") == "printing"), None)


def on_job_finished(result: str, status: dict | None, photo: str | None, note: str = "") -> dict | None:
    hist = load_store("history", [])
    rec = next((h for h in reversed(hist) if h.get("result") == "printing"), None)
    if rec is None:
        return None
    rec.update(result=result, ended_at=ts_human(), ended_epoch=time.time(),
               duration_s=int(time.time() - rec.get("started_epoch", time.time())), final_photo=photo, note=note)
    if status and status.get("elapsed_s"):
        rec["duration_s"] = int(status["elapsed_s"])
    save_store("history", hist)
    m = load_store("maintenance", {})
    m["print_hours"] = round(m.get("print_hours", 0) + rec["duration_s"] / 3600, 3)
    m["prints"] = m.get("prints", 0) + 1
    save_store("maintenance", m)
    if rec.get("queue_id"):
        _queue_set(rec["queue_id"], status="done" if result == "success" else "failed")
    if result == "success" and rec.get("est_g") and rec.get("color"):
        _inventory_consume(rec.get("material"), rec.get("color"), rec["est_g"])
    return rec


def cmd_history(a, out):
    hist = load_store("history", [])
    if a.history_cmd == "list":
        out["history"] = hist[-a.limit:]
    elif a.history_cmd == "stats":
        done = [h for h in hist if h.get("result") in ("success", "failed", "cancelled")]
        ok = [h for h in done if h["result"] == "success"]
        grams = sum(h.get("est_g") or 0 for h in ok)
        out["stats"] = {"jobs": len(hist), "finished": len(done), "success": len(ok),
                        "failed_or_cancelled": len(done) - len(ok),
                        "success_rate_pct": round(100 * len(ok) / len(done), 1) if done else None,
                        "print_hours": round(sum(h.get("duration_s") or 0 for h in done) / 3600, 2),
                        "filament_g_est": round(grams, 1),
                        "cost_usd_est": round(sum(h.get("est_cost") or 0 for h in ok), 2),
                        "open_job": open_job()}
    elif a.history_cmd == "mark":
        rec = next((h for h in hist if h["id"] == a.id), None)
        if not rec:
            raise Fail(f"no history id {a.id}")
        rec["result"] = a.result
        if a.note:
            rec["note"] = a.note
        save_store("history", hist)
        out["updated"] = rec


# ============================================================ filament inventory
def _inventory_consume(material, color, grams) -> None:
    inv = load_store("inventory", [])
    for sp in inv:
        if not sp.get("archived") and sp.get("color", "").lower() == str(color).lower() and \
                (not material or sp.get("material", "").upper() == str(material).upper()):
            sp["grams_left"] = max(0, round(float(sp.get("grams_left", 0)) - float(grams), 1))
            save_store("inventory", inv)
            return


def inventory_check(material: str | None, color: str | None, grams: float | None) -> dict:
    inv = [s for s in load_store("inventory", []) if not s.get("archived")]
    if not inv:
        return {"ok": None, "note": "inventory empty: add spools with `filament add` to enable color checks"}
    hits = [s for s in inv if (not color or s.get("color", "").lower() == color.lower())
            and (not material or s.get("material", "").upper() == material.upper())]
    if not hits:
        return {"ok": False, "note": f"no {material or ''} {color or ''} spool on hand".replace("  ", " ")}
    best = max(hits, key=lambda s: float(s.get("grams_left", 0)))
    if grams and float(best.get("grams_left", 0)) < grams * 1.1:
        return {"ok": False, "spool": best, "note": f"only {best.get('grams_left')} g left, job needs ~{grams} g"}
    return {"ok": True, "spool": best}


def cmd_filament(a, out):
    inv = load_store("inventory", [])
    c = a.filament_cmd
    if c == "list":
        out["spools"] = [s for s in inv if a.all or not s.get("archived")]
        low = env_float("FILAMENT_LOW_G", 150)
        out["low"] = [s for s in out["spools"] if not s.get("archived") and float(s.get("grams_left", 0)) < low]
    elif c == "add":
        sp = {"id": f"s{len(inv) + 1:03d}", "material": a.material.upper(), "color": a.color.lower(),
              "brand": a.brand, "grams_left": a.grams, "cost_per_kg": a.cost, "added_at": ts_human(), "archived": False}
        inv.append(sp)
        save_store("inventory", inv)
        out["added"] = sp
    elif c in ("use", "set", "archive"):
        sp = next((s for s in inv if s["id"] == a.id), None)
        if not sp:
            raise Fail(f"no spool {a.id}")
        if c == "use":
            sp["grams_left"] = max(0, round(float(sp["grams_left"]) - a.grams, 1))
        elif c == "set":
            sp["grams_left"] = a.grams
        else:
            sp["archived"] = True  # never deleted
        save_store("inventory", inv)
        out["spool"] = sp
    elif c == "check":
        out["check"] = inventory_check(a.material, a.color, a.grams)
        if out["check"]["ok"] is False:
            out["warnings"].append(out["check"]["note"])


# ============================================================ print queue / job manager
def _queue_set(qid: str, **kw) -> None:
    q = load_store("queue", [])
    for it in q:
        if it["id"] == qid:
            it.update(kw)
            it["updated_at"] = ts_human()
    save_store("queue", q)


def _queue_add_file(q: list, f: Path, a) -> dict:
    ins = inspect_gcode(f, a.material)
    it = {"id": f"q{int(time.time() * 1000) % 10_000_000:07d}{len(q)}", "file": str(f), "name": a.name or f.name,
          "color": (a.color or "").lower() or None, "material": (a.material or ins.get("material") or "PETG").upper(),
          "note": a.note, "status": "queued", "added_at": ts_human(), "est_s": ins.get("estimated_s"),
          "est_g": ins.get("filament_g"), "est_cost": ins.get("cost_usd"), "warnings": ins["warnings"]}
    q.append(it)
    return it


def cmd_queue(a, out):
    q = load_store("queue", [])
    c = a.queue_cmd
    if c == "add":
        added = []
        for raw in a.files:
            f = Path(raw)
            if f.is_dir():
                files = sorted(f.glob("plate_*.gcode"), key=lambda p: int(re.findall(r"\d+", p.stem)[0]))
                if not files:
                    raise Fail(f"no plate_*.gcode in {f}")
            elif f.suffix.lower() == ".gcode" and f.is_file():
                files = [f]
            else:
                raise Fail(f"{f}: queue takes sliced .gcode (or a slice output folder). Slice 3MF/STL first.")
            for g in files:
                added.append(_queue_add_file(q, g, a))
        save_store("queue", q)
        out["added"] = added
        for it in added:
            chk = inventory_check(it["material"], it["color"], it["est_g"]) if it["color"] else {"ok": None}
            if chk.get("ok") is False:
                out["warnings"].append(f"{it['name']}: {chk['note']}")
    elif c == "list":
        out["queue"] = [it for it in q if a.all or it["status"] not in ("done", "skipped")]
        pend = [it for it in out["queue"] if it["status"] == "queued"]
        out["remaining_est"] = fmt_dur(sum(it.get("est_s") or 0 for it in pend))
        out["remaining_g"] = round(sum(it.get("est_g") or 0 for it in pend), 1)
    elif c == "skip":
        _queue_set(a.id, status="skipped")
        out["skipped"] = a.id
    elif c == "move":
        it = next((x for x in q if x["id"] == a.id), None)
        if not it:
            raise Fail(f"no queue item {a.id}")
        q.remove(it)
        q.insert(max(0, a.to - 1), it)
        save_store("queue", q)
        out["queue"] = [x["id"] for x in q]
    elif c == "next":
        live = [x for x in q if x["status"] in ("printing", "awaiting_owner")]
        if live and not a.force:
            raise Fail(f"{live[0]['name']} is {live[0]['status']}; finish it (or pass --force)")
        it = next((x for x in q if x["status"] == "queued"), None)
        if not it:
            out["note"] = "queue empty"
            return
        prev = next((x for x in reversed(q) if x["status"] == "done"), None)
        swap = None
        if it.get("color") and (not prev or prev.get("color") != it["color"]):
            swap = f"load {it['color']} {it['material']}" + (f" (was {prev['color']})" if prev and prev.get("color") else "")
        chk = inventory_check(it["material"], it["color"], it["est_g"]) if it.get("color") else {"ok": None}
        if chk.get("ok") is False:
            out["warnings"].append(chk["note"])
        cfg = printer_cfg()
        up = upload_gcode(cfg, Path(it["file"]), safe_name(Path(it["file"]).stem + f"_{it['id']}.gcode"))
        out["upload"] = up
        ns = argparse.Namespace(file_on_printer=up["uploaded"], gcode_root=None)
        note = " | ".join(x for x in (swap and f"FILAMENT: {swap}", it.get("note")) if x)
        request_gated("start", ns, out, reason=f"Queue job {it['name']}" + (f" ({note})" if note else ""),
                      extra={"_params": {"queue_id": it["id"], "local_gcode": it["file"], "color": it.get("color"),
                                         "material": it.get("material"), "swap_note": swap}})
        _queue_set(it["id"], status="awaiting_owner", request_id=out.get("request_id"))
        out["job"] = it


# ============================================================ plates by color
_OBJ = re.compile(rb"<object\b[^>]*\bid=\"(\d+)\"[^>]*>.*?</object>", re.S)
_ITEM = re.compile(rb"<item\b[^>]*objectid=\"(\d+)\"[^>]*/>")


def split_3mf_by_color(src: Path, mapping: dict, default: str, outdir: Path) -> list[dict]:
    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        if "3D/3dmodel.model" not in names:
            raise Fail("no 3D/3dmodel.model in this 3MF")
        model = z.read("3D/3dmodel.model")
        others = {n: z.read(n) for n in names if n != "3D/3dmodel.model"}
    if any(n.startswith("3D/Objects/") for n in names) or b"p:path=" in model:
        raise Fail("this is a Creality/Bambu project 3MF (objects in separate files); assign colors in the GUI, "
                   "or export the parts as a plain 3MF/STL first")
    objs = {m.group(1).decode(): m for m in _OBJ.finditer(model)}
    by_color: dict = {}
    for oid, m in objs.items():
        nm = re.search(rb'\bname="([^"]*)"', m.group(0)[:400])
        name = nm.group(1).decode() if nm else f"object_{oid}"
        color = next((v for k, v in mapping.items() if k.lower() in name.lower()), default).lower()
        by_color.setdefault(color, []).append((oid, name))
    head = model[:model.index(b"<resources>") + len(b"<resources>")]
    results = []
    for color, items in by_color.items():
        keep = {oid for oid, _ in items}
        body = b"".join(objs[o].group(0) for o in objs if o in keep)
        build = b"".join(m.group(0) for m in _ITEM.finditer(model) if m.group(1).decode() in keep)
        new = head + body + b"</resources><build>" + build + b"</build></model>"
        dst = outdir / f"{src.stem}_{safe_name(color)}.3mf"
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
            zo.writestr("3D/3dmodel.model", new)
            for n, d in others.items():
                if not n.startswith("Metadata/"):
                    zo.writestr(n, d)
        results.append({"color": color, "file": str(dst), "objects": [n for _, n in items]})
    return results


def cmd_plates(a, out):
    src = Path(a.file)
    if not src.is_file():
        raise Fail(f"not found: {src}")
    mapping = {}
    for pair in (a.map or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            mapping[k.strip()] = v.strip()
    outdir = Path(a.outdir or (state_dir() / "plates" / f"{safe_name(src.stem)}_{ts_compact()}"))
    outdir.mkdir(parents=True, exist_ok=True)
    plates = split_3mf_by_color(src, mapping, a.default, outdir)
    order = [c.lower() for c in (a.order or "").split(",") if c.strip()]
    plates.sort(key=lambda p: order.index(p["color"]) if p["color"] in order else len(order))
    out["plates"] = plates
    out["plan"] = [f"{i + 1}. print {', '.join(p['objects'])} in {p['color']}" for i, p in enumerate(plates)]
    out["next"] = ("slice each file (slice <file> --arrange), then `queue add <outputdir> --color <color>`; "
                   "the queue asks for the filament swap between jobs")


# ============================================================ preset recommender
RECIPES = {
    "kids-toy": {"walls": 4, "top": 5, "bottom": 5, "infill": 20, "pattern": "gyroid", "brim": 5, "supports": "off",
                    "nozzle": 240, "first_nozzle": 235, "bed": 75,
                    "notes": ["one-piece parts, nothing that fits the 31.7 x 57.1 mm choking cylinder",
                              "PETG is not food safe; hand wash only (softens ~70-80 C)",
                              "4 walls + 5 top/bottom so chewed edges don't crack"]},
    "car-exterior": {"walls": 5, "top": 6, "bottom": 6, "infill": 40, "pattern": "gyroid", "brim": 8, "supports": "auto",
                     "nozzle": 245, "first_nozzle": 240, "bed": 80,
                     "notes": ["PETG softens ~75-80 C: black parts in summer sun can sag; ASA is the better outdoor material",
                               "sand + filler primer + UV clear if painted; 3M 94 primer under VHB tape on a bare pad",
                               "add hidden screws on anything in the airflow"]},
    "car-interior": {"walls": 4, "top": 5, "bottom": 5, "infill": 30, "pattern": "gyroid", "brim": 5, "supports": "auto",
                     "nozzle": 240, "first_nozzle": 235, "bed": 75,
                     "notes": ["dashboard tops get hotter than 80 C in sun; keep PETG parts out of direct windshield sun"]},
    "functional": {"walls": 5, "top": 5, "bottom": 5, "infill": 40, "pattern": "cubic", "brim": 5, "supports": "auto",
                   "nozzle": 240, "first_nozzle": 235, "bed": 75, "notes": ["orient so layer lines don't carry the main load"]},
    "decor": {"walls": 3, "top": 4, "bottom": 4, "infill": 15, "pattern": "gyroid", "brim": 0, "supports": "off",
              "layer_height": 0.16, "nozzle": 240, "first_nozzle": 235, "bed": 75, "notes": ["0.16 mm layers for a cleaner surface"]},
    "flat-sign": {"walls": 3, "top": 5, "bottom": 4, "infill": 100, "pattern": "rectilinear", "brim": 0, "supports": "off",
                  "nozzle": 240, "first_nozzle": 235, "bed": 75, "ironing": True,
                  "notes": ["thin letters/plates: 100% infill, ironing on top"]},
    "tall-thin": {"walls": 4, "top": 5, "bottom": 5, "infill": 25, "pattern": "gyroid", "brim": 8, "supports": "off",
                  "nozzle": 240, "first_nozzle": 235, "bed": 75, "notes": ["8 mm brim; slow down if it wobbles"]},
}
MATERIAL_TEMPS = {"PETG": (240, 235, 75), "PLA": (210, 205, 60), "ABS": (245, 240, 100), "ASA": (250, 245, 100), "TPU": (225, 220, 50)}


def cmd_recommend(a, out):
    part = a.part
    if part not in RECIPES:
        raise Fail(f"unknown part type; choose: {', '.join(RECIPES)}")
    r = dict(RECIPES[part])
    mat = a.material.upper()
    if mat != "PETG" and mat in MATERIAL_TEMPS:
        r["nozzle"], r["first_nozzle"], r["bed"] = MATERIAL_TEMPS[mat]
    proc = {"wall_loops": str(r["walls"]), "top_shell_layers": str(r["top"]), "bottom_shell_layers": str(r["bottom"]),
            "sparse_infill_density": f"{r['infill']}%", "sparse_infill_pattern": r["pattern"],
            "brim_width": str(r["brim"]), "brim_type": "outer_only" if r["brim"] else "no_brim",
            "enable_support": "0" if r["supports"] == "off" else "1"}
    if r.get("layer_height"):
        proc["layer_height"] = str(r["layer_height"])
    if r.get("ironing"):
        proc["ironing_type"] = "top"
    fil = {"nozzle_temperature": str(r["nozzle"]), "nozzle_temperature_initial_layer": str(r["first_nozzle"]),
           "hot_plate_temp": str(r["bed"]), "textured_plate_temp": str(r["bed"])}
    out["part"], out["material"] = part, mat
    out["process_overrides"] = proc
    out["filament_overrides"] = fil
    out["notes"] = r["notes"]
    sets = " ".join(f"--set {k}={v}" for k, v in proc.items())
    out["slice_command"] = f'slice "<file>" {sets}'
    out["make_presets"] = [f'presets clone "{DEFAULT_PROCESS}" "TRM {part}" ' + sets,
                           f'presets clone "{DEFAULT_FILAMENT if mat == "PETG" else "<your " + mat + " filament>"}" "TRM {mat} {r["nozzle"]}" '
                           + " ".join(f"--set {k}={v}" for k, v in fil.items())]


# ============================================================ maintenance
MAINT_TASKS = {  # task: (every N print hours, every N prints) - None = not used
    "clean_bed": (None, 10), "cold_pull": (50, None), "check_hotend_fan": (100, None),
    "belt_tension": (100, None), "tighten_frame_bolts": (150, None), "lube_z_rods": (200, None),
    "nozzle_replace": (250, None), "dry_filament": (None, 15),
}
JAM_CHECKLIST = [
    "heat creep: is the hotend (heatsink) fan spinning at full speed whenever the nozzle is hot?",
    "retraction <= 1.5 mm (0.8 mm at 40 mm/s works for PETG on the SE)",
    "dry the PETG: 65 C for 4-6 h (wet PETG pops, strings and clogs)",
    "print PETG at 235-245 C, not the 250+ max",
    "hot-tighten the nozzle at ~240 C so filament can't leak around the threads",
    "do 2-3 cold pulls (heat to 240, push, cool to ~90, pull)",
    "check the extruder gears / tension and the PTFE path for chewed filament",
]


def maint_status() -> dict:
    m = load_store("maintenance", {})
    hours, prints = m.get("print_hours", 0), m.get("prints", 0)
    due = []
    for task, (h_every, p_every) in MAINT_TASKS.items():
        last = (m.get("last_done") or {}).get(task, {"hours": 0, "prints": 0})
        h_since, p_since = hours - last.get("hours", 0), prints - last.get("prints", 0)
        is_due = (h_every and h_since >= h_every) or (p_every and p_since >= p_every)
        soon = (h_every and h_since >= 0.8 * h_every) or (p_every and p_since >= 0.8 * p_every)
        due.append({"task": task, "status": "due" if is_due else ("soon" if soon else "ok"),
                    "hours_since": round(h_since, 1), "prints_since": p_since, "every_hours": h_every, "every_prints": p_every})
    return {"print_hours": round(hours, 2), "prints": prints,
            "nozzle_hours": round(hours - m.get("nozzle_installed_hours", 0), 2),
            "tasks": due, "due": [d["task"] for d in due if d["status"] == "due"],
            "jams_logged": len(m.get("jams", [])), "recent_log": (m.get("log") or [])[-5:]}


def cmd_maint(a, out):
    m = load_store("maintenance", {})
    c = a.maint_cmd
    if c == "done":
        if a.task not in MAINT_TASKS:
            raise Fail(f"unknown task; choose: {', '.join(MAINT_TASKS)}")
        m.setdefault("last_done", {})[a.task] = {"hours": m.get("print_hours", 0), "prints": m.get("prints", 0), "at": ts_human()}
        if a.task == "nozzle_replace":
            m["nozzle_installed_hours"] = m.get("print_hours", 0)
        m.setdefault("log", []).append({"at": ts_human(), "task": a.task, "note": a.note})
        save_store("maintenance", m)
    elif c == "jam":
        m.setdefault("jams", []).append({"at": ts_human(), "note": a.note, "print_hours": m.get("print_hours", 0)})
        save_store("maintenance", m)
        out["checklist"] = JAM_CHECKLIST
        recent = [j for j in m["jams"] if j.get("print_hours", 0) >= m.get("print_hours", 0) - 20]
        if len(recent) >= 3:
            out["warnings"].append(f"{len(recent)} jams in the last ~20 print hours: check heat creep and filament moisture first")
    elif c == "add-hours":
        m["print_hours"] = round(m.get("print_hours", 0) + a.hours, 3)
        save_store("maintenance", m)
    out["maintenance"] = maint_status()


# ============================================================ file library
SKIP_DIRS = {".git", "node_modules", "__pycache__", "_backup", ".venv", "venv", "state"}


def cmd_library(a, out):
    lib_path = _store("library")
    if a.library_cmd == "scan":
        roots = [Path(r) for r in (a.root or [])] or [Path(p) for p in env("LIBRARY_ROOTS").split(";") if p] or [ROOT.parent.parent]
        rows, skipped = [], 0
        for root in roots:
            for dirpath, dirnames, files in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for fn in files:
                    if not fn.lower().endswith((".3mf", ".stl")):
                        continue
                    f = Path(dirpath) / fn
                    if len(rows) >= a.limit:
                        break
                    row = {"file": str(f), "name": fn, "mb": round(f.stat().st_size / 1e6, 2),
                           "modified": _dt.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
                    if f.stat().st_size <= a.max_mb * 1e6:
                        try:
                            sub = {"warnings": [], "errors": []}
                            cmd_split_check(argparse.Namespace(file=str(f), bed=a.bed, bed_y=None, max_z=float(env("MAX_Z_MM") or 250)), sub)
                            row.update(size_mm=sub["size_mm"], fits=sub["fits_as_is"] or sub["fits_rotated_z"],
                                       pieces=sub["suggested_pieces"])
                        except Exception as e:
                            row["error"] = str(e)[:120]
                    else:
                        skipped += 1
                        row["note"] = f"over {a.max_mb} MB, dims not read"
                    rows.append(row)
        save_store("library", {"scanned_at": ts_human(), "roots": [str(r) for r in roots], "bed": a.bed, "files": rows})
        out.update(count=len(rows), too_big_to_measure=skipped, index=str(lib_path),
                   not_fitting=[r["name"] for r in rows if r.get("fits") is False][:50])
    else:
        lib = load_store("library", None)
        if not lib:
            raise Fail("no library index yet; run `library scan`")
        words = (a.text or "").lower().split()
        hits = [r for r in lib["files"] if all(w in r["file"].lower() for w in words)
                and (not a.fits or r.get("fits")) and (not a.too_big or r.get("fits") is False)]
        out.update(scanned_at=lib["scanned_at"], count=len(hits), files=hits[:100])


# ============================================================ timelapse
def cmd_timelapse(a, out):
    snaps_dir = state_dir() / "snapshots"
    t0, t1 = 0.0, time.time()
    if a.history_id:
        rec = next((h for h in load_store("history", []) if h["id"] == a.history_id), None)
        if not rec:
            raise Fail(f"no history id {a.history_id}")
        t0, t1 = rec.get("started_epoch", 0) - 60, rec.get("ended_epoch", time.time()) + 120
    elif a.since:
        t0 = _dt.datetime.strptime(a.since, "%Y-%m-%d %H:%M").timestamp()
    frames = sorted((p for p in snaps_dir.glob("*.jpg") if t0 <= p.stat().st_mtime <= t1), key=lambda p: p.stat().st_mtime)
    if len(frames) < 2:
        raise Fail(f"need at least 2 snapshots in that window, found {len(frames)} (watch takes them while printing)")
    dst_dir = state_dir() / "timelapse"
    dst_dir.mkdir(parents=True, exist_ok=True)
    ff = shutil.which("ffmpeg")
    if ff and not a.gif:
        lst = dst_dir / f"frames_{ts_compact()}.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\nduration {1 / a.fps:.4f}\n" for p in frames), encoding="utf-8")
        dst = dst_dir / f"timelapse_{ts_compact()}_ET.mp4"
        r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p", str(dst)],
                           capture_output=True, text=True, timeout=600)
        lst.unlink(missing_ok=True)
        if r.returncode != 0:
            raise Fail(f"ffmpeg failed: {r.stderr[-300:]}")
        out.update(video=str(dst), frames=len(frames), tool="ffmpeg")
        return
    Image = _pil()
    if not Image:
        raise Fail("need ffmpeg on PATH or Pillow for a GIF")
    ims = []
    for p in frames[-400:]:
        im = Image.open(p).convert("RGB")
        im.thumbnail((640, 480))
        ims.append(im)
    dst = dst_dir / f"timelapse_{ts_compact()}_ET.gif"
    ims[0].save(dst, save_all=True, append_images=ims[1:], duration=int(1000 / a.fps), loop=0)
    out.update(video=str(dst), frames=len(ims), tool="pillow-gif", note="install ffmpeg for mp4")


# ============================================================ printer extras (read-only queries, console)
READONLY_GCODE = {"M105", "M114", "M115", "M119", "M27", "M31", "M503", "M20"}
PRINTER_QUERIES = {"files": "reqGcodeFile", "history": "reqHistory", "mesh": "reqProbedMatrix",
                   "objects": "reqPrintObjects", "timelapses": "reqElapseVideoList", "config": "reqPrinterCfg",
                   "ota": "reqOtaInfo", "materials": "reqMaterials"}


def ws_query(cfg: dict, key: str, wait: float = 4.0, until=None) -> dict:
    d = ws_exchange(cfg, [{"method": "get", "params": {key: 1}}], wait=wait, until=until)
    return {k: v for k, v in d.items() if k not in STATUS_PUSH_KEYS and k not in STATUS_RAW_KEYS}


STATUS_RAW_KEYS = ("state", "deviceState", "printFileName", "printProgress", "printJobTime", "printLeftTime",
                   "nozzleTemp", "targetNozzleTemp", "bedTemp0", "targetBedTemp0", "layer", "TotalLayer",
                   "modelFanPct", "auxiliaryFanPct", "caseFanPct", "curPosition", "err", "model", "hostname",
                   "modelVersion", "maxNozzleTemp", "maxBedTemp", "boxTemp", "curFeedratePct", "curFlowratePct")


def _parse_file_info(raw) -> list:
    """K1 sends a list (retGcodeFileInfo2). A real CR-10 SE (DWIN V1.1.0.28) sends retGcodeFileInfo =
    {"totalNum": n, "fileInfo": "dir:name:size:?:ctime:?:thumb;dir:name:..."}."""
    if isinstance(raw, dict):
        raw = raw.get("fileInfo") or raw.get("list") or []
    if isinstance(raw, str):
        out = []
        for ent in raw.split(";"):
            parts = ent.split(":")
            if len(parts) >= 3 and parts[1]:
                ct = _f(parts[4]) if len(parts) > 4 else None
                out.append({"name": parts[1], "path": f"{parts[0].rstrip('/')}/{parts[1]}",
                            "size": int(_f(parts[2]) or 0),
                            "created_at": ts_human(_dt.datetime.fromtimestamp(ct, now_et().tzinfo)) if ct else None})
        return out
    return raw if isinstance(raw, list) else []


def printer_files(cfg: dict) -> list:
    d = ws_query(cfg, "reqGcodeFile", wait=float(env("PRINTER_FILES_WAIT_S") or 6),
                 until=lambda v: "retGcodeFileInfo2" in v or "retGcodeFileInfo" in v)
    return _parse_file_info(d.get("retGcodeFileInfo2") or d.get("retGcodeFileInfo") or [])


# keys the printer pushes with every status message (seen on a real CR-10 SE); stripped from query replies
STATUS_PUSH_KEYS = set("""TotalLayer accelToDecelLimits accelerationLimits aiDetection aiFirstFloor aiPausePrint aiSw
autoLevelResult autohome auxiliaryFanPct bedTemp0 bedTemp1 bedTemp2 bedTempAutoPid boxTemp caseFanPct connect
cornerVelocityLimits curFeedratePct curFlowratePct curPosition dProgress deviceState enableSelfTest err fan
fanAuxiliary fanCase hostname layer lightSw materialDetect materialStatus maxBedTemp maxNozzleTemp model modelFanPct
modelVersion nozzleMoveSnapshot nozzleTemp nozzleTempAutoPid powerLoss pressureAdvance printFileName printId
printJobTime printLeftTime printProgress printStartTime realTimeFlow realTimeSpeed repoPlrStatus smoothTime state
targetBedTemp0 targetBedTemp1 targetBedTemp2 targetNozzleTemp tfCard upgradeStatus usedMaterialLength velocityLimits
video video1 videoElapse videoElapseFrame videoElapseInterval withSelfTest""".split())


def cmd_printer_extra(a, out):
    cfg = printer_cfg()
    sub = a.printer_cmd
    out["printer_ip"] = cfg["ip"]
    if sub in PRINTER_QUERIES:
        if sub == "files":
            out["files"] = printer_files(cfg)
            out["count"] = len(out["files"])
        else:
            out["reply"] = ws_query(cfg, PRINTER_QUERIES[sub])
        out["verified_on_cr10se"] = False
        if not out.get("files") and not out.get("reply"):
            out["warnings"].append(f"printer sent nothing for {PRINTER_QUERIES[sub]} (may not exist on this firmware)")
    elif sub == "info":
        info = {}
        try:
            with urllib.request.urlopen(f"http://{cfg['ip']}:{cfg['http_port']}/info", timeout=4) as r:
                info["http_info"] = json.loads(r.read(20000) or b"{}")
        except Exception as e:
            info["http_info_error"] = str(e)[:150]
        try:
            st = printer_status(cfg)
            info.update(model=st.get("model"), hostname=st.get("hostname"), firmware=st.get("firmware"))
            info["ota"] = ws_query(cfg, "reqOtaInfo", wait=2)
        except Exception as e:
            info["ws_error"] = str(e)[:150]
        dd = data_dir()
        if dd and (dd / "deviceInfo.json").is_file():
            j = _read_json(dd / "deviceInfo.json") or {}
            info["creality_print_saved"] = [{k: d.get(k) for k in ("address", "model", "name", "mac")}
                                            for g in j.get("groups", []) or [] for d in g.get("list", []) or []]
        out["info"] = info
    elif sub == "light":
        ws_exchange(cfg, [{"method": "set", "params": {"lightSw": 1 if a.state == "on" else 0}}], wait=1.5)
        out["light"] = a.state
        out["note"] = "the CR-10 SE may not have a controllable light; harmless if ignored"
    elif sub == "gcode":
        cmd = a.gcode_line.strip()
        word = cmd.split()[0].upper() if cmd else ""
        if word in READONLY_GCODE and "\n" not in cmd:
            d = ws_exchange(cfg, [{"method": "set", "params": {"gcodeCmd": cmd}}], wait=3)
            out.update(sent=cmd, readonly=True, reply={k: v for k, v in d.items() if k not in STATUS_RAW_KEYS},
                       note="Creality OS may not echo gcode replies over ws; temps/position are in `printer status`")
        else:
            request_gated("gcode", a, out)


# ============================================================ health report
def cmd_health(a, out):
    rep: dict = {}
    exe = find_exe()
    rep["slicer"] = {"exe": str(exe) if exe else None, "version": exe.parent.name if exe else None,
                     "gui_open": bool(gui_running())}
    lp = _latest_path()
    if a.probe:
        try:
            printer_status(printer_cfg())
        except Exception as e:
            out["warnings"].append(f"printer not reachable: {str(e)[:120]}")
    if lp.is_file():
        j = _read_json(lp) or {}
        age = round(time.time() - lp.stat().st_mtime)
        rep["printer"] = {"age_s": age, "stale": age > 600, **{k: j.get(k) for k in (
            "state", "progress_pct", "layer", "total_layers", "remaining_s", "nozzle", "bed", "file", "has_error", "error")}}
        if j.get("printing") and j.get("remaining_s"):
            rep["printer"]["finish_at_et"] = ts_human(now_et() + _dt.timedelta(seconds=float(j["remaining_s"])))
    else:
        rep["printer"] = {"note": "no live data yet (printer status / stream)"}
    rep["printer_ip"] = discover_printer_ip()
    rep["pending_approvals"] = [{"request_id": r.get("request_id"), "action": r.get("action"), "created_at": r.get("created_at")}
                               for r in (_read_json(p) or {} for p in _pending_dir().glob("*.json"))]
    q = load_store("queue", [])
    rep["queue"] = {"queued": sum(1 for x in q if x["status"] == "queued"),
                    "active": [x["name"] for x in q if x["status"] in ("printing", "awaiting_owner")]}
    low = env_float("FILAMENT_LOW_G", 150)
    inv = [s for s in load_store("inventory", []) if not s.get("archived")]
    rep["filament"] = {"spools": len(inv), "low": [f"{s['color']} {s['material']} ({s['grams_left']} g)" for s in inv
                                                   if float(s.get("grams_left", 0)) < low]}
    ms = maint_status()
    rep["maintenance"] = {"print_hours": ms["print_hours"], "due": ms["due"]}
    hist = load_store("history", [])
    done = [h for h in hist if h.get("result") in ("success", "failed", "cancelled")]
    rep["history"] = {"jobs": len(hist), "success": sum(1 for h in done if h["result"] == "success"),
                      "failed": sum(1 for h in done if h["result"] != "success"), "last": hist[-1] if hist else None}
    alerts = sorted(owner_inbox_dir().glob(f"{BOT} - ALERT*.json"))[-3:]
    rep["recent_alerts"] = [((_read_json(p) or {}).get("context") or {}).get("summary") for p in alerts]
    rep["camera"] = {"configured": camera_enabled(), "empty_bed_reference": reference_path().is_file()}
    rep["telegram"] = bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"))
    rep["auto_pause_on_anomaly"] = env_bool("AUTO_PAUSE_ON_ANOMALY", False)
    size = sum(p.stat().st_size for p in state_dir().rglob("*") if p.is_file())
    rep["state_mb"] = round(size / 1e6, 1)
    flags = []
    if rep["slicer"]["gui_open"]:
        flags.append("Creality Print window open (CLI slicing blocked)")
    if rep["printer"].get("stale"):
        flags.append("live printer data is stale")
    if rep["printer"].get("has_error"):
        flags.append(f"printer error {rep['printer'].get('error')}")
    flags += [f"maintenance due: {t}" for t in ms["due"]]
    flags += [f"low filament: {x}" for x in rep["filament"]["low"]]
    rep["flags"] = flags
    out["health"] = rep
    out["summary"] = "; ".join(flags) if flags else "all good"
    if a.packet:
        out["packet"] = str(write_packet("health report", "report", out["summary"], rep, "ok"))


# ============================================================ watch job events (ETA, first layer, done)
def watch_job_events(status: dict, ws_state: dict, snap: dict | None, result: dict) -> list:
    events = []
    cur, prev = status.get("state"), ws_state.get("last_state")
    printing = status.get("printing")
    if printing and prev not in ("printing", "paused"):
        ws_state.update(first_layer_done=False, eta_notified=False, job_file=status.get("file"))
        if not open_job():
            on_job_started({"file": status.get("file")})
        events.append("job_started")
    if printing and not ws_state.get("first_layer_done"):
        layer = status.get("layer")
        prog = status.get("progress_pct") or 0
        if (isinstance(layer, (int, float)) and layer >= 2) or (layer is None and prog >= 3):
            ws_state["first_layer_done"] = True
            photo = None
            if camera_enabled():
                try:
                    photo = take_snapshot("first_layer")
                except Exception:
                    photo = None
            n = notify("first layer check", f"First layer done ({ts_human()}). Check it looks smooth and stuck down.",
                       {"snapshot": photo, "file": status.get("file")}, photo=photo and photo.get("path"))
            events.append({"first_layer": n, "snapshot": photo})
    rem = status.get("remaining_s")
    if printing and rem:
        finish = now_et() + _dt.timedelta(seconds=float(rem))
        result["eta"] = {"remaining": fmt_dur(rem), "finish_at_et": ts_human(finish)}
        mins = env_float("ETA_NOTIFY_MIN", 10)
        if rem <= mins * 60 and not ws_state.get("eta_notified"):
            ws_state["eta_notified"] = True
            events.append({"eta": notify("almost done", f"About {fmt_dur(rem)} left on {status.get('file')}; done ~{finish:%I:%M %p} ET.",
                                         result["eta"])})
    if cur == "paused" and prev == "printing":
        job = open_job()
        q = next((x for x in load_store("queue", []) if job and x["id"] == job.get("queue_id")), None)
        events.append({"paused": notify("printer paused", f"Printer paused at {ts_human()} (filament change or pause). "
                                        + (f"Job note: {q['note']}. " if q and q.get("note") else "")
                                        + "Resume needs the owner's yes.", {"status": status},
                                        photo=snap and snap.get("path"), priority="high")})
    if prev in ("printing", "paused") and cur in ("complete", "failed", "cancelled"):
        final = None
        if camera_enabled():
            try:
                final = take_snapshot("final")
            except Exception:
                final = None
        res = "success" if cur == "complete" else cur
        rec = on_job_finished(res, status, final and final.get("path"))
        nxt = next((x for x in load_store("queue", []) if x["status"] == "queued"), None)
        text = (f"Print {'done' if res == 'success' else res.upper()} at {ts_human()}: {status.get('file')}. "
                f"Clear the bed before the next print.")
        if nxt:
            text += f" Next in queue: {nxt['name']}" + (f" in {nxt['color']}" if nxt.get("color") else "") + \
                    (f" ({nxt['note']})" if nxt.get("note") else "") + ". Say 'next' to queue it up."
        events.append({"finished": notify(f"print {res}", text, {"history": rec, "final_photo": final, "next": nxt},
                                          photo=final and final.get("path"), priority="high")})
    ws_state["last_state"] = cur
    return events


# ============================================================ CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="printer_remote", description="Creality Print + CR-10 SE agent bridge (JSON out)")
    p.add_argument("--version", action="version", version=VERSION)
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("detect", help="find Creality Print, presets, GUI state, printer IP")

    pr = sp.add_parser("presets", help="list/show/clone/set presets").add_subparsers(dest="presets_cmd", required=True)
    x = pr.add_parser("list")
    x.add_argument("--type", choices=PRESET_TYPES)
    x.add_argument("--filter")
    x.add_argument("--all", action="store_true", help="include non-instantiable base presets")
    x = pr.add_parser("show")
    x.add_argument("name")
    x.add_argument("--type", choices=PRESET_TYPES)
    x.add_argument("--resolved", action="store_true", help="flatten inherits chain")
    x.add_argument("--keys", nargs="*")
    x = pr.add_parser("clone")
    x.add_argument("source")
    x.add_argument("new_name")
    x.add_argument("--type", choices=PRESET_TYPES)
    x.add_argument("--set", action="append")
    x.add_argument("--account", help="user subfolder (default: 'default')")
    x = pr.add_parser("set", help="edit a USER preset (backed up first); system presets are refused")
    x.add_argument("name")
    x.add_argument("--type", choices=PRESET_TYPES)
    x.add_argument("--set", action="append")

    x = sp.add_parser("slice", help="slice 3mf/stl with Creality Print CLI")
    x.add_argument("input")
    x.add_argument("--outputdir")
    x.add_argument("--plate", type=int, default=0, help="0 = all plates")
    x.add_argument("--printer")
    x.add_argument("--process")
    x.add_argument("--filament", action="append")
    x.add_argument("--set", action="append", help="key=value override (alias names ok: walls, infill, brim, supports)")
    x.add_argument("--layer-height", dest="layer_height", type=float)
    x.add_argument("--walls", type=int)
    x.add_argument("--infill", help="percent, e.g. 20")
    x.add_argument("--brim", type=float, help="brim width mm (0 = none)")
    x.add_argument("--supports", choices=("on", "off"))
    x.add_argument("--arrange", action="store_true")
    x.add_argument("--orient", action="store_true")
    x.add_argument("--datadir")
    x.add_argument("--flag-style", dest="flag_style", choices=("auto", "underscore", "dash"),
                   default=env("CREALITY_FLAG_STYLE") or "auto")
    x.add_argument("--timeout", type=int, default=900)
    x.add_argument("--allow-gui", action="store_true")
    x.add_argument("--close-gui", action="store_true", help="close a running GUI first (unsaved work is lost)")
    x.add_argument("--keep-temp", action="store_true")

    x = sp.add_parser("open", help="open a file in the Creality Print GUI")
    x.add_argument("file")

    x = sp.add_parser("split-check", help="does a mesh fit the bed; suggested piece count")
    x.add_argument("file")
    x.add_argument("--bed", type=float, default=float(env("BED_USABLE_MM") or 215))
    x.add_argument("--bed-y", dest="bed_y", type=float)
    x.add_argument("--max-z", dest="max_z", type=float, default=float(env("MAX_Z_MM") or 250))

    pp = sp.add_parser("printer", help="LAN: read-only queries + upload; gated: start/pause/resume/cancel/heat/home/move/fan")
    ps = pp.add_subparsers(dest="printer_cmd", required=True)
    for n in ("discover", "probe", "status", "latest", "pause", "resume", "cancel", "home", "info",
              "files", "history", "mesh", "objects", "timelapses", "config", "ota", "materials"):
        ps.add_parser(n)
    x = ps.add_parser("move", help="gated: jog the head (absolute within the bed, or --relative up to 50 mm)")
    for ax in ("x", "y", "z"):
        x.add_argument(f"--{ax}", type=float)
    x.add_argument("--relative", action="store_true")
    x.add_argument("--feed", type=int, default=3000, help="mm/min")
    x = ps.add_parser("fan", help="gated: part fan percent")
    x.add_argument("--pct", type=float)
    x = ps.add_parser("light")
    x.add_argument("state", choices=("on", "off"))
    x = ps.add_parser("gcode", help="console: read-only queries (M105 M114 M115 M119 M27 M31 M503 M20) free, anything else gated")
    x.add_argument("gcode_line", metavar="GCODE")
    x = ps.add_parser("stream", help="live ws 9999 subscription; updates state/printer_latest.json + rolling log")
    x.add_argument("--seconds", type=float, default=30, help="how long to listen (Ctrl+C stops early)")
    x.add_argument("--jsonl", action="store_true", help="print one JSON line per update, then a summary line")
    x.add_argument("--min-interval", dest="min_interval", type=float, default=1.0)
    x = ps.add_parser("upload")
    x.add_argument("file")
    x.add_argument("--name")
    x = ps.add_parser("start")
    x.add_argument("file_on_printer", help="gcode filename already uploaded")
    x.add_argument("--gcode-root", dest="gcode_root")
    x.add_argument("--color", help="filament color for the summary and the inventory preflight")
    x.add_argument("--material", help="filament material (e.g. PETG)")
    x = ps.add_parser("heat")
    x.add_argument("--nozzle", type=float)
    x.add_argument("--bed", type=float)

    x = sp.add_parser("approve", help="record the OWNER's yes/no for a request; yes runs preflight, then executes")
    x.add_argument("request_id")
    g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--yes", action="store_true")
    g.add_argument("--no", action="store_true")
    x.add_argument("--by", default="owner_chat", help="where the answer came from (e.g. owner_chat, telegram)")
    x.add_argument("--quote", help="the owner's own words, verbatim")
    x = sp.add_parser("preflight", help="run the go/no-go checks for a pending request without executing")
    x.add_argument("request_id")
    sp.add_parser("pending", help="list requests awaiting the owner's answer")

    cp = sp.add_parser("camera", help="snapshot / empty-bed reference / bed-clear check").add_subparsers(dest="camera_cmd", required=True)
    x = cp.add_parser("snapshot")
    x.add_argument("--label", default="snap")
    x = cp.add_parser("set-reference")
    x.add_argument("--from-file", dest="from_file")
    cp.add_parser("bed-check")

    x = sp.add_parser("watch", help="malfunction watch: snapshots + heuristics -> owner_inbox/Telegram alerts")
    x.add_argument("--once", action="store_true", help="one tick (for a scheduled job)")
    x.add_argument("--interval", type=int, default=int(env("WATCH_INTERVAL_S") or 120))
    x.add_argument("--max-minutes", dest="max_minutes", type=float, default=720)
    x.add_argument("--stop-when-idle", dest="stop_when_idle", action="store_true")
    x.add_argument("--reset", action="store_true")
    x = sp.add_parser("gcode", help="inspect a gcode file: temps, layers, time, grams, cost, warnings")
    gsp = x.add_subparsers(dest="gcode_cmd", required=True)
    y = gsp.add_parser("inspect")
    y.add_argument("file")
    y.add_argument("--material")

    hp = sp.add_parser("history", help="print history + stats").add_subparsers(dest="history_cmd", required=True)
    y = hp.add_parser("list")
    y.add_argument("--limit", type=int, default=20)
    hp.add_parser("stats")
    y = hp.add_parser("mark")
    y.add_argument("id")
    y.add_argument("--result", choices=("success", "failed", "cancelled"), required=True)
    y.add_argument("--note")

    fp = sp.add_parser("filament", help="spool inventory").add_subparsers(dest="filament_cmd", required=True)
    y = fp.add_parser("list")
    y.add_argument("--all", action="store_true")
    y = fp.add_parser("add")
    y.add_argument("--material", required=True)
    y.add_argument("--color", required=True)
    y.add_argument("--grams", type=float, default=1000)
    y.add_argument("--cost", type=float, help="$/kg")
    y.add_argument("--brand")
    for n in ("use", "set"):
        y = fp.add_parser(n)
        y.add_argument("id")
        y.add_argument("--grams", type=float, required=True)
    y = fp.add_parser("archive")
    y.add_argument("id")
    y = fp.add_parser("check")
    y.add_argument("--material")
    y.add_argument("--color")
    y.add_argument("--grams", type=float)

    qp = sp.add_parser("queue", help="job queue with filament-swap steps").add_subparsers(dest="queue_cmd", required=True)
    y = qp.add_parser("add")
    y.add_argument("files", nargs="+", help=".gcode files or slice output folders (plate_*.gcode)")
    y.add_argument("--color")
    y.add_argument("--material")
    y.add_argument("--note")
    y.add_argument("--name")
    y = qp.add_parser("list")
    y.add_argument("--all", action="store_true")
    y = qp.add_parser("skip")
    y.add_argument("id")
    y = qp.add_parser("move")
    y.add_argument("id")
    y.add_argument("--to", type=int, required=True)
    y = qp.add_parser("next", help="upload the next job and request its start (gated)")
    y.add_argument("--force", action="store_true")

    x = sp.add_parser("plates", help="split a plain 3MF into one file per color")
    psp = x.add_subparsers(dest="plates_cmd", required=True)
    y = psp.add_parser("by-color")
    y.add_argument("file")
    y.add_argument("--map", help='"egg_top=white,egg_bottom=white,yolk=red"')
    y.add_argument("--default", default="white")
    y.add_argument("--order", help="print order of colors, e.g. white,red")
    y.add_argument("--outdir")

    x = sp.add_parser("recommend", help="settings for a material + part type")
    x.add_argument("--material", default="PETG")
    x.add_argument("--part", required=True, help=", ".join(RECIPES))

    mp = sp.add_parser("maint", help="maintenance tracker").add_subparsers(dest="maint_cmd", required=True)
    mp.add_parser("status")
    y = mp.add_parser("done")
    y.add_argument("task")
    y.add_argument("--note")
    y = mp.add_parser("jam", help="log a jam and get the checklist")
    y.add_argument("--note")
    y = mp.add_parser("add-hours")
    y.add_argument("hours", type=float)

    lp = sp.add_parser("library", help="index of 3mf/stl files with dims + fit").add_subparsers(dest="library_cmd", required=True)
    y = lp.add_parser("scan")
    y.add_argument("--root", action="append")
    y.add_argument("--bed", type=float, default=float(env("BED_USABLE_MM") or 215))
    y.add_argument("--max-mb", dest="max_mb", type=float, default=60)
    y.add_argument("--limit", type=int, default=3000)
    y = lp.add_parser("find")
    y.add_argument("text", nargs="?")
    y.add_argument("--fits", action="store_true")
    y.add_argument("--too-big", dest="too_big", action="store_true")

    x = sp.add_parser("timelapse", help="build a timelapse from watch snapshots")
    tsp = x.add_subparsers(dest="timelapse_cmd", required=True)
    y = tsp.add_parser("build")
    y.add_argument("--history-id", dest="history_id")
    y.add_argument("--since", help='"YYYY-MM-DD HH:MM" (ET, box clock)')
    y.add_argument("--fps", type=float, default=10)
    y.add_argument("--gif", action="store_true")

    x = sp.add_parser("health", help="one summary of everything for the agent")
    x.add_argument("--probe", action="store_true", help="refresh live printer data first")
    x.add_argument("--packet", action="store_true", help="also write an owner_inbox report packet")
    return p


HANDLERS = {"gcode": cmd_gcode, "history": cmd_history, "filament": cmd_filament, "queue": cmd_queue,
            "plates": cmd_plates, "recommend": cmd_recommend, "maint": cmd_maint, "library": cmd_library,
            "timelapse": cmd_timelapse, "health": cmd_health,"detect": cmd_detect, "presets": cmd_presets, "slice": cmd_slice, "open": cmd_open,
            "split-check": cmd_split_check, "printer": cmd_printer, "approve": cmd_approve, "preflight": cmd_preflight,
            "pending": cmd_pending, "camera": cmd_camera, "watch": cmd_watch}


def main(argv=None) -> int:
    load_env()
    args = build_parser().parse_args(argv)
    sub = next((getattr(args, k) for k in ("presets_cmd", "printer_cmd", "camera_cmd", "gcode_cmd", "history_cmd",
                                           "filament_cmd", "queue_cmd", "plates_cmd", "maint_cmd", "library_cmd",
                                           "timelapse_cmd") if getattr(args, k, None)), None)
    out = {"status": "ok", "job_id": new_job_id(), "command": args.cmd + (f" {sub}" if sub else ""),
           "started_at": now_et().isoformat(timespec="seconds"), "errors": [], "warnings": []}
    code = 0
    try:
        HANDLERS[args.cmd](args, out)
    except Fail as e:
        out["status"] = "error"
        out["errors"].append(str(e))
    except Exception as e:  # never dump a traceback at the agent; keep JSON
        out["status"] = "error"
        out["errors"].append(f"{type(e).__name__}: {e}")
    if out["status"] == "error":
        code = 1
    out["finished_at"] = now_et().isoformat(timespec="seconds")
    log_job({k: out.get(k) for k in ("job_id", "command", "status", "errors", "started_at", "finished_at")})
    jsonl = getattr(args, "jsonl", False)
    sys.stdout.write(json.dumps(out, indent=None if jsonl else 2, default=str) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
