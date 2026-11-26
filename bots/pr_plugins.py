"""Pluggable interfaces (part of the single printer_remote CLI):
- Slicer backends: Creality Print (working, the 1.x slicer) + OrcaSlicer / PrusaSlicer CLI stubs (unverified).
- Failure detectors: the 1.x image diff + an HTTP hook for an ML model (Obico / Spaghetti Detective style; documented,
  not bundled).
- Recovery playbooks: notify, pause, cooldown, cancel, requeue on another printer. Every physical step goes through
  request_gated, so the autonomy policy applies; only thermal/error kinds may use the urgent cooldown.
- Printer backends: Creality OS websocket (default) + a Moonraker HTTP backend for rooted printers (fake-tested only).
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import printer_remote as core


# ================================================================ slicers
class SlicerBackend:
    name = "base"
    exe_env = ""
    verified = False

    def exe(self) -> str | None:
        return core.env(self.exe_env) or None

    def available(self) -> bool:
        e = self.exe()
        return bool(e and Path(e).exists())

    def build_argv(self, src: Path, outdir: Path, configs: list[str]) -> list[str]:
        raise NotImplementedError

    def slice(self, src: Path, outdir: Path, configs: list[str], timeout: int = 900) -> dict:
        if not self.available():
            raise core.Fail(f"{self.name}: set {self.exe_env} to the slicer executable")
        outdir.mkdir(parents=True, exist_ok=True)
        argv = self.build_argv(src, outdir, configs)
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        gcodes = sorted(str(p) for p in outdir.glob("*.gcode"))
        return {"backend": self.name, "argv": argv, "returncode": r.returncode, "gcode": gcodes,
                "log_tail": (r.stdout + r.stderr)[-1500:], "verified_backend": self.verified}


class CrealityPrintSlicer(SlicerBackend):
    name = "creality"
    exe_env = "CREALITY_PRINT_EXE"
    verified = True  # the 1.x `slice` command, used for real prints

    def available(self) -> bool:
        return core.find_exe() is not None

    def slice(self, src: Path, outdir: Path, configs: list[str], timeout: int = 900) -> dict:
        argv = [sys.executable, str(core.HERE / "printer_remote.py"), "slice", str(src), "--outputdir", str(outdir),
                "--timeout", str(timeout)]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 60, env=os.environ.copy())
        try:
            j = json.loads(r.stdout)
        except ValueError:
            raise core.Fail(f"creality slice gave non-JSON output: {r.stdout[-300:]} {r.stderr[-300:]}")
        return {"backend": self.name, "result": j, "gcode": j.get("gcode") or j.get("outputs"), "verified_backend": True}


class OrcaSlicer(SlicerBackend):
    """OrcaSlicer CLI (Bambu-style flags). Stub: argv shape from OrcaSlicer docs, not run on hardware here."""
    name = "orca"
    exe_env = "ORCASLICER_EXE"

    def build_argv(self, src, outdir, configs):
        machine = [c for c in configs if "machine" in c.lower()]
        process = [c for c in configs if "process" in c.lower()]
        fil = [c for c in configs if "filament" in c.lower()]
        argv = [self.exe(), "--slice", "0", "--outputdir", str(outdir)]
        if machine or process:
            argv += ["--load-settings", ";".join(machine + process)]
        if fil:
            argv += ["--load-filaments", ";".join(fil)]
        return argv + [str(src)]


class PrusaSlicer(SlicerBackend):
    """PrusaSlicer CLI. Stub: `--export-gcode --load <ini> --output <file>`."""
    name = "prusa"
    exe_env = "PRUSASLICER_EXE"

    def build_argv(self, src, outdir, configs):
        argv = [self.exe(), "--export-gcode"]
        for c in configs:
            argv += ["--load", c]
        return argv + ["--output", str(outdir / (src.stem + ".gcode")), str(src)]


SLICERS = {c.name: c for c in (CrealityPrintSlicer(), OrcaSlicer(), PrusaSlicer())}


def get_slicer(name: str) -> SlicerBackend:
    if name not in SLICERS:
        raise core.Fail(f"unknown slicer {name}; have {list(SLICERS)}")
    return SLICERS[name]


# ================================================================ detectors
class Detector:
    name = "base"

    def analyze(self, frame: str, ctx: dict) -> dict:
        raise NotImplementedError


class ImageDiffDetector(Detector):
    """1.x heuristic: big change between consecutive frames = possible crash/spaghetti; no change while printing = stall."""
    name = "imagediff"

    def analyze(self, frame, ctx):
        prev = ctx.get("previous")
        if not prev:
            return {"detector": self.name, "anomaly": False, "score": None, "detail": "no previous frame"}
        d = core.image_diff(prev, frame)
        if d is None:
            return {"detector": self.name, "anomaly": False, "score": None, "detail": "Pillow/numpy missing"}
        spike = core.env_float("WATCH_DIFF_SPIKE", 20)
        stall = core.env_float("WATCH_STALL_DIFF", 0.4)
        kind = "big_frame_change" if d >= spike else ("no_motion" if ctx.get("printing") and d < stall else None)
        return {"detector": self.name, "anomaly": kind == "big_frame_change", "kind": kind, "score": d,
                "threshold": spike}


class HttpMLDetector(Detector):
    """Hook for an external ML failure detector. Not bundled.
    DETECTOR_ML_URL      base URL of the model server
    DETECTOR_ML_MODE     post (default): POST the JPEG, expects {"detections": [[label, conf, box], ...]} or {"score": x}
                         obico: GET <url>/p/?img=<image url> (Obico ml_api); needs ctx image_url reachable by the server
    DETECTOR_ML_THRESHOLD  confidence that counts as failure (default 0.6)"""
    name = "ml"

    def analyze(self, frame, ctx):
        url = core.env("DETECTOR_ML_URL")
        if not url:
            return {"detector": self.name, "anomaly": False, "skipped": "DETECTOR_ML_URL not set"}
        thr = core.env_float("DETECTOR_ML_THRESHOLD", 0.6)
        mode = (core.env("DETECTOR_ML_MODE") or "post").lower()
        try:
            if mode == "obico":
                if not ctx.get("image_url"):
                    return {"detector": self.name, "anomaly": False, "skipped": "obico mode needs an image URL"}
                q = urllib.parse.urlencode({"img": ctx["image_url"]})
                with urllib.request.urlopen(f"{url.rstrip('/')}/p/?{q}", timeout=core.env_float("DETECTOR_ML_TIMEOUT_S", 20)) as r:
                    j = json.loads(r.read() or b"{}")
            else:
                req = urllib.request.Request(url, data=Path(frame).read_bytes(), method="POST",
                                             headers={"Content-Type": "image/jpeg"})
                with urllib.request.urlopen(req, timeout=core.env_float("DETECTOR_ML_TIMEOUT_S", 20)) as r:
                    j = json.loads(r.read() or b"{}")
        except (OSError, ValueError) as e:
            return {"detector": self.name, "anomaly": False, "error": str(e)[:160]}
        dets = j.get("detections") or []
        best = max((float(d[1]) for d in dets if isinstance(d, (list, tuple)) and len(d) > 1), default=None)
        score = float(j["score"]) if "score" in j else best
        return {"detector": self.name, "anomaly": score is not None and score >= thr, "kind": "ml_failure" if score and score >= thr else None,
                "score": score, "threshold": thr, "detections": dets[:10]}


DETECTORS = {d.name: d for d in (ImageDiffDetector(), HttpMLDetector())}


def run_detectors(frame: str, ctx: dict, names: list[str] | None = None) -> dict:
    names = names or [n for n in (core.env("DETECTORS") or "imagediff,ml").split(",") if n in DETECTORS]
    res = [DETECTORS[n].analyze(frame, ctx) for n in names]
    hits = [r for r in res if r.get("anomaly")]
    return {"anomaly": bool(hits), "kinds": [r.get("kind") for r in hits], "results": res}


# ================================================================ recovery playbooks
DEFAULT_PLAYBOOKS = {
    "spaghetti": ["notify", "pause", "requeue"],
    "ml_failure": ["notify", "pause", "requeue"],
    "big_frame_change": ["notify", "pause"],
    "layer_shift": ["notify", "pause", "requeue"],
    "thermal": ["notify", "cooldown"],
    "printer_error": ["notify", "cooldown", "requeue"],
    "job_lost": ["notify", "requeue"],
    "power_loss_resume_pending": ["notify"],
    "default": ["notify"],
}
URGENT_KINDS = ("thermal", "printer_error", "nozzle_temp_runaway", "bed_temp_runaway", "nozzle_heating_without_target")


def playbook_for(kind: str) -> list[str]:
    pol = core._mod("pr_policy").load_policy()
    pb = {**DEFAULT_PLAYBOOKS, **(pol.get("playbooks") or {})}
    return list(pb.get(kind) or pb["default"])


def run_playbook(kind: str, printer: str | None, detail: str = "", dry_run: bool = False, job_id: str | None = None) -> dict:
    steps = playbook_for(kind)
    done = []
    prev = core.active_printer()
    core.set_active_printer(printer)
    try:
        for step in steps:
            if dry_run:
                done.append({"step": step, "dry_run": True})
                continue
            sub = {"status": "ok", "warnings": [], "errors": [], "job_id": job_id or core.new_job_id()}
            try:
                if step == "notify":
                    done.append({"step": step, "alert": core._mod("pr_alerts").emit("anomaly", f"{kind}: {detail}",
                                                                                   {"kind": kind}, printer=printer, key=kind)})
                elif step in ("pause", "cancel"):
                    core.request_gated(step, argparse.Namespace(), sub, reason=f"Playbook {kind}: {detail}")
                    done.append({"step": step, "status": sub["status"], "request_id": sub.get("request_id"), "errors": sub["errors"]})
                elif step == "cooldown":
                    if kind in URGENT_KINDS:
                        done.append({"step": step, "urgent": core.urgent_cooldown(f"playbook {kind}: {detail}", "playbook")})
                    else:
                        core.request_gated("cooldown", argparse.Namespace(), sub, reason=f"Playbook {kind}: {detail}")
                        done.append({"step": step, "status": sub["status"], "request_id": sub.get("request_id")})
                elif step == "requeue":
                    farm = core._mod("pr_farm")
                    jobs = farm.load_jobs()
                    live = [j for j in jobs if j["status"] in ("printing", "awaiting_approval") and j.get("printer") == (printer or "default")]
                    res = [farm.mark_finished(jobs, j["id"], False, f"playbook {kind}") for j in live]
                    farm.save_jobs(jobs)
                    done.append({"step": step, "requeued": res,
                                 "note": "the part still on the bed must be cleared before that printer takes new work"})
                else:
                    done.append({"step": step, "error": "unknown step"})
            except core.Fail as e:
                done.append({"step": step, "error": str(e)})
        core._mod("pr_policy").audit("playbook", printer=printer, detail=f"{kind}: {[d['step'] for d in done]}")
    finally:
        core.set_active_printer(prev)
    return {"kind": kind, "printer": printer, "steps": done}


# ================================================================ Moonraker backend (rooted printers)
class MoonrakerBackend:
    """Moonraker HTTP API. Fake-tested only; no rooted printer has been tried."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = f"http://{cfg['ip']}:{cfg.get('moonraker_port') or 7125}"
        k = cfg.get("moonraker_api_key_env")
        self.key = core.env(k) if k else ""

    def _req(self, method: str, path: str, data: bytes | None = None, ctype: str | None = None, timeout: float = 8):
        h = {"X-Api-Key": self.key} if self.key else {}
        if ctype:
            h["Content-Type"] = ctype
        last = None
        for delay in [0.0] + core.backoff_delays(1 if method == "GET" else 0, 0.5):
            if delay:
                import time as _t
                _t.sleep(delay)
            try:
                with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, method=method, headers=h), timeout=timeout) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                raise core.Fail(f"moonraker {path}: HTTP {e.code}")
            except OSError as e:
                last = e
        raise core.Fail(f"moonraker unreachable: {last}")

    def status(self) -> dict:
        q = "print_stats&extruder&heater_bed&virtual_sdcard&fan&webhooks"
        st = (self._req("GET", f"/printer/objects/query?{q}").get("result") or {}).get("status") or {}
        ps, ex, hb = st.get("print_stats") or {}, st.get("extruder") or {}, st.get("heater_bed") or {}
        state = ps.get("state") or "unknown"
        err = state == "error" or (st.get("webhooks") or {}).get("state") not in (None, "ready")
        printing = state in ("printing", "paused")
        return {"received_at": core.ts_human(), "state": {"standby": "idle"}.get(state, state), "state_code": state,
                "printing": printing, "paused": state == "paused", "idle": not printing,
                "file": ps.get("filename"), "progress_pct": round(100 * float((st.get("virtual_sdcard") or {}).get("progress") or 0), 1),
                "elapsed_s": ps.get("print_duration"),
                "nozzle": {"actual": ex.get("temperature"), "target": ex.get("target"), "max": None},
                "bed": {"actual": hb.get("temperature"), "target": hb.get("target"), "max": None},
                "fans_pct": {"part": round(100 * float((st.get("fan") or {}).get("speed") or 0), 1)},
                "error": {"code": 1 if err else 0, "raw": ps.get("message") or (st.get("webhooks") or {}).get("state_message")},
                "has_error": bool(err), "power_loss_recovery": None, "backend": "moonraker"}

    def gcode(self, script: str) -> dict:
        return self._req("POST", "/printer/gcode/script?" + urllib.parse.urlencode({"script": script}))

    def execute(self, action: str, params: dict) -> dict:
        if action == "start":
            st = self.status()
            if st.get("printing"):
                raise core.Fail("printer is already printing; not starting another job")
            r = self._req("POST", "/printer/print/start?" + urllib.parse.urlencode({"filename": params["file"]}))
            rec = core.on_job_started({**params, "printer": core.active_printer()})
            return {"backend": "moonraker", "sent": "print/start", "file": params["file"], "history_id": rec["id"], "reply": r}
        if action in ("pause", "resume", "cancel"):
            return {"backend": "moonraker", "sent": f"print/{action}", "reply": self._req("POST", f"/printer/print/{action}")}
        if action == "heat":
            lines = ([f"M104 S{int(params['nozzle'])}"] if params.get("nozzle") is not None else []) + \
                    ([f"M140 S{int(params['bed'])}"] if params.get("bed") is not None else [])
        elif action == "cooldown":
            lines = ["M104 S0", "M140 S0"]
        elif action == "home":
            lines = ["G28"]
        elif action == "fan":
            lines = [f"M106 S{int(params.get('pct', 0) * 255 / 100 + 0.5)}"]
        elif action == "move":
            lines = params["gcode"]
        elif action == "gcode":
            lines = [params["cmd"]]
        else:
            raise core.Fail(f"{action} is not implemented for the Moonraker backend yet")
        return {"backend": "moonraker", "sent": lines, "reply": self.gcode("\n".join(lines))}

    def upload(self, f: Path, name: str) -> dict:
        boundary = "----trm" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + f.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        r = self._req("POST", "/server/files/upload", body, f"multipart/form-data; boundary={boundary}", timeout=600)
        return {"uploaded": name, "backend": "moonraker", "reply": r, "mb": round(f.stat().st_size / 1e6, 2)}


def moonraker_for(cfg: dict) -> MoonrakerBackend:
    return MoonrakerBackend(cfg)


# ================================================================ CLI
def cmd_slicer(a, out):
    if a.slicer_cmd == "list":
        out["slicers"] = [{"name": s.name, "env": s.exe_env, "available": s.available(), "verified": s.verified}
                          for s in SLICERS.values()]
    else:
        s = get_slicer(a.backend)
        outdir = Path(a.outputdir or (core.state_dir() / "gcode" / f"{core.safe_name(Path(a.input).stem)}_{a.backend}_{core.ts_compact()}"))
        out["slice"] = s.slice(Path(a.input), outdir, a.config or [], a.timeout)
        if not s.verified:
            out["warnings"].append(f"{s.name} backend is a stub: check the gcode before printing it")


def cmd_detector(a, out):
    if a.detect_cmd == "list":
        out["detectors"] = list(DETECTORS)
        out["playbooks"] = {**DEFAULT_PLAYBOOKS, **(core._mod("pr_policy").load_policy().get("playbooks") or {})}
        return
    ctx = {"previous": a.previous, "printing": a.printing, "image_url": a.image_url}
    out["detection"] = run_detectors(a.image, ctx, a.detector.split(",") if a.detector else None)


def cmd_playbook(a, out):
    if a.playbook_cmd == "list":
        out["playbooks"] = {**DEFAULT_PLAYBOOKS, **(core._mod("pr_policy").load_policy().get("playbooks") or {})}
        return
    out["playbook"] = run_playbook(a.kind, a.printer_name or core.active_printer(), a.detail or "", a.dry_run)


def register(sp, handlers: dict) -> None:
    x = sp.add_parser("slicer", help="pluggable slicers: creality (working), orca / prusa (stubs)")
    s = x.add_subparsers(dest="slicer_cmd", required=True)
    s.add_parser("list")
    y = s.add_parser("slice")
    y.add_argument("input")
    y.add_argument("--backend", default="creality", choices=list(SLICERS))
    y.add_argument("--outputdir")
    y.add_argument("--config", action="append", help="settings file(s) for orca/prusa")
    y.add_argument("--timeout", type=int, default=900)
    handlers["slicer"] = cmd_slicer
    x = sp.add_parser("detector", help="failure detectors: imagediff (built in) + ML hook (DETECTOR_ML_URL)")
    s = x.add_subparsers(dest="detect_cmd", required=True)
    s.add_parser("list")
    y = s.add_parser("run")
    y.add_argument("--image", required=True)
    y.add_argument("--previous")
    y.add_argument("--printing", action="store_true")
    y.add_argument("--image-url", dest="image_url")
    y.add_argument("--detector", help="comma list (default DETECTORS env or imagediff,ml)")
    handlers["detector"] = cmd_detector
    x = sp.add_parser("playbook", help="recovery playbooks: notify / pause / cooldown / cancel / requeue (policy-gated)")
    s = x.add_subparsers(dest="playbook_cmd", required=True)
    s.add_parser("list")
    y = s.add_parser("run")
    y.add_argument("--kind", required=True)
    y.add_argument("--printer", dest="printer_name")
    y.add_argument("--detail")
    y.add_argument("--dry-run", dest="dry_run", action="store_true")
    handlers["playbook"] = cmd_playbook
