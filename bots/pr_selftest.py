"""`selftest hardware`: walk every action not yet proven on the real printer, one gated step at a time, and record the
result in bots/state/selftest_results.json and docs/PROTOCOL.md (between the SELFTEST markers; IPs are scrubbed).

Gated steps create a normal approval request (never auto, whatever the policy says); after the owner's yes and the
preflight, the step is verified from a fresh status read and recorded. `--record` lets the owner record what they saw.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import printer_remote as core

# id: (action, kind, needs state, safe while nobody is at the printer, what it does)
STEPS = {
    "light": ("light", "free", "any", True, "light off then on"),
    "fan": ("fan", "gated", "any", True, "part fan to --pct (default 60), then set it back yourself"),
    "heat": ("heat", "gated", "any", True, "nozzle target to --nozzle (small change mid-print, or a low idle preheat)"),
    "pause": ("pause", "gated", "printing", True, "pause a running print"),
    "resume": ("resume", "gated", "paused", True, "resume a paused print (it reheats first)"),
    "cooldown": ("cooldown", "gated", "idle", True, "nozzle and bed targets to 0 (idle only for this test)"),
    "upload": ("upload", "free", "any", True, "upload a small .gcode (--file) and check it is listed"),
    "start": ("start", "gated", "idle", False, "start an uploaded file (--file-on-printer); someone must watch the bed"),
    "cancel": ("cancel", "gated", "printing", False, "cancel a print (use a throwaway test print)"),
    "home": ("home", "gated", "idle", False, "G28 home all axes; bed must be clear"),
    "move": ("move", "gated", "idle", False, "relative Z +5 mm (--z), after homing"),
    "load": ("load", "gated", "idle", False, "filament load (heats, feeds, purges); someone at the printer"),
}

# Already proven on the real CR-10 SE (DWIN V1.1.0.28), 2026-11-26. Times are ET.
KNOWN_HARDWARE = [
    {"step": "retract", "result": "pass", "at": "2026-11-26 13:29 ET", "note": "filament retract 10 mm at 300 C, owner approved"},
    {"step": "unload", "result": "pass", "at": "2026-11-26 14:00 ET", "note": "unload 80 mm at 300 C; owner saw filament come out"},
    {"step": "extrude", "result": "pass", "at": "2026-11-26 14:09 ET", "note": "extrude 30 mm at 300 C"},
    {"step": "start", "result": "pass", "at": "2026-11-26 14:21 ET", "note": "started a white PETG test print (opGcodeFile)"},
    {"step": "light", "result": "pass", "at": "2026-11-26 15:29 ET", "note": "lightSw 0 then 1 mid-print; light_on followed within 5 s"},
    {"step": "fan", "result": "pass", "at": "2026-11-26 15:30 ET", "note": "M106 S179 -> part fan 70% (held 20 s, slicer did not override), M106 S128 -> 50%"},
    {"step": "heat", "result": "pass", "at": "2026-11-26 15:30 ET", "note": "mid-print M104 S235: target 235 at once, actual 235 in ~25 s; M104 S240 restored, 240 in ~30 s"},
    {"step": "pause", "result": "pass", "at": "2026-11-26 15:31 ET", "note": "{pause:1} -> state 5 paused; firmware parks head X0 Y222, lifts Z ~46 mm, drops nozzle to 140 C, keeps bed 80 C"},
    {"step": "resume", "result": "pass", "at": "2026-11-26 15:33 ET", "note": "{pause:0} after 77 s paused; reheats 140->240 C (~75 s, state 0 + deviceState 1), back to printing at 15:34:59"},
]

BEGIN, END = "<!-- SELFTEST:BEGIN -->", "<!-- SELFTEST:END -->"
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _results_path() -> Path:
    return core.state_dir() / "selftest_results.json"


def _doc_path() -> Path:
    return Path(core.env("PR_PROTOCOL_DOC") or (core.ROOT / "docs" / "PROTOCOL.md"))


def scrub(s) -> str:
    return _IP.sub("<printer-ip>", str(s or ""))


def load_results() -> list[dict]:
    return core._read_json(_results_path()) or []


def record(step: str, result: str, note: str = "", data: dict | None = None) -> dict:
    if result not in ("pass", "fail", "skip", "needs_eye_check"):
        raise core.Fail("result must be pass, fail, skip or needs_eye_check")
    rows = load_results()
    rec = {"step": step, "result": result, "at": core.ts_human(), "printer": core.active_printer() or "default",
           "note": scrub(note)[:400], "data": json.loads(scrub(json.dumps(data or {}, default=str)))}
    rows.append(rec)
    core.atomic_write(_results_path(), json.dumps(rows, indent=2))
    write_doc()
    core._mod("pr_policy").audit("selftest_record", printer=core.active_printer(), detail=f"{step}: {result}")
    return rec


def checklist() -> list[dict]:
    rows = load_results()
    out = []
    for sid, (action, kind, needs, unattended_ok, what) in STEPS.items():
        mine = [r for r in rows if r["step"] == sid]
        known = [k for k in KNOWN_HARDWARE if k["step"] == sid]
        last = (mine or known or [None])[-1]
        out.append({"step": sid, "action": action, "kind": kind, "needs_state": needs, "ok_unattended": unattended_ok,
                    "what": what, "verified": bool(last and last["result"] == "pass"),
                    "last": last and {k: last.get(k) for k in ("result", "at", "note")}})
    return out


def write_doc() -> None:
    p = _doc_path()
    if not p.is_file():
        return
    s = p.read_text(encoding="utf-8")
    lines = [BEGIN, "", "| step | result | when (ET) | note |", "|---|---|---|---|"]
    for k in KNOWN_HARDWARE:
        lines.append(f"| {k['step']} | {k['result']} | {k['at'].replace(' ET', '')} | {scrub(k['note'])} |")
    for r in load_results():
        lines.append(f"| {r['step']} | {r['result']} | {r['at'].replace(' ET', '')} | {scrub(r['note']).replace('|', '/')} |")
    lines += ["", END]
    block = "\n".join(lines)
    if BEGIN in s and END in s:
        s = s[:s.index(BEGIN)] + block + s[s.index(END) + len(END):]
    else:
        s = s.rstrip() + "\n\n## Hardware verification log (`selftest hardware`)\n\n" + block + "\n"
    core.atomic_write(p, s)


def _state_ok(needs: str, st: dict) -> tuple[bool, str]:
    if needs == "any":
        return True, "any state"
    if needs == "idle":
        return bool(st.get("idle")), f"needs idle, printer is {st.get('state')}"
    if needs == "printing":
        return bool(st.get("printing") and not st.get("paused")), f"needs a running print, printer is {st.get('state')}"
    if needs == "paused":
        return bool(st.get("paused")), f"needs a paused print, printer is {st.get('state')}"
    return True, ""


def run_step(sid: str, a, out: dict) -> None:
    if sid not in STEPS:
        raise core.Fail(f"unknown step {sid}; steps: {', '.join(STEPS)}")
    action, kind, needs, _, what = STEPS[sid]
    cfg = core.printer_cfg()
    st = core.printer_status(cfg)
    ok, why = _state_ok(needs, st)
    if not ok:
        raise core.Fail(f"selftest {sid}: {why}")
    base = {k: st.get(k) for k in ("state", "nozzle", "bed", "fans_pct", "position", "light_on", "progress_pct")}
    if sid == "light":
        before = st.get("light_on")
        core.ws_exchange(cfg, [{"method": "set", "params": {"lightSw": 0}}], wait=1.5)
        time.sleep(2)
        off = core.printer_status(cfg).get("light_on")
        core.ws_exchange(cfg, [{"method": "set", "params": {"lightSw": 1 if before is not False else 0}}], wait=1.5)
        time.sleep(2)
        back = core.printer_status(cfg).get("light_on")
        res = "pass" if off is False and back == (before is not False) else ("needs_eye_check" if off is None else "fail")
        out["selftest"] = record(sid, res, f"light_on {before} -> {off} -> {back}")
        return
    if sid == "upload":
        if not a.file or not Path(a.file).is_file():
            raise core.Fail("selftest upload needs --file <small .gcode>")
        name = core.safe_name("selftest_" + Path(a.file).name)
        up = core.upload_gcode(cfg, Path(a.file), name)
        names = [str(x.get("name") or "") for x in core.printer_files(cfg) if isinstance(x, dict)]
        hit = any(n.endswith(name) for n in names)
        out["upload"] = up
        out["selftest"] = record(sid, "pass" if hit else "fail", f"uploaded {name} ({up.get('mb')} MB); listed={hit}")
        return
    ns = argparse.Namespace(file_on_printer=a.file_on_printer, gcode_root=None, nozzle=a.nozzle, bed=None, pct=a.pct,
                            x=None, y=None, z=a.z, relative=True, feed=600, mm=None, material=a.material, temp=None,
                            purge=False, speed=None, color=None)
    if action == "start" and not a.file_on_printer:
        raise core.Fail("selftest start needs --file-on-printer <name already uploaded>")
    if action == "heat" and a.nozzle is None:
        raise core.Fail("selftest heat needs --nozzle <C> (e.g. 5 below the running target)")
    if action == "fan" and a.pct is None:
        ns.pct = 60.0
    if action == "move" and a.z is None:
        ns.z = 5.0
    core.request_gated(action, ns, out, reason=f"Hardware self-test step '{sid}': {what}",
                       extra={"_params": {"selftest_step": sid, "selftest_baseline": base}})


def verify_after(req: dict) -> dict:
    """Called by approve after a self-test step executed. Polls status briefly and records the verdict."""
    sid = req["params"]["selftest_step"]
    p = req["params"]
    base = p.get("selftest_baseline") or {}
    cfg = core.printer_cfg()
    deadline = time.time() + core.env_float("SELFTEST_VERIFY_S", 45)
    verdict, note, st = "fail", "", {}
    while time.time() < deadline:
        try:
            st = core.printer_status(cfg)
        except Exception as e:
            note = f"status read failed: {str(e)[:100]}"
            time.sleep(2)
            continue
        if sid == "start" and st.get("printing"):
            verdict, note = "pass", f"printing {st.get('file')}"
        elif sid == "pause" and st.get("paused"):
            verdict, note = "pass", f"paused; nozzle {st['nozzle']}, head {st.get('position')}"
        elif sid == "resume" and not st.get("paused") and st.get("printing"):
            verdict, note = "pass", f"resumed ({st.get('state')}), nozzle {st['nozzle']}"
        elif sid == "cancel" and st.get("idle"):
            verdict, note = "pass", f"idle ({st.get('state')})"
        elif sid == "heat" and (st.get("nozzle") or {}).get("target") == float(p.get("nozzle")):
            verdict, note = "pass", f"nozzle target {p.get('nozzle')} accepted (actual {st['nozzle'].get('actual')})"
        elif sid == "fan" and abs(float((st.get("fans_pct") or {}).get("part") or -99) - float(p.get("pct"))) <= 2:
            verdict, note = "pass", f"part fan {st['fans_pct']['part']}%"
        elif sid == "cooldown" and (st.get("nozzle") or {}).get("target") in (0, 0.0) and (st.get("bed") or {}).get("target") in (0, 0.0):
            verdict, note = "pass", "nozzle and bed targets 0"
        elif sid == "move":
            z0 = ((base.get("position") or {}) if isinstance(base.get("position"), dict) else {}).get("Z")
            z1 = (st.get("position") or {}).get("Z") if isinstance(st.get("position"), dict) else None
            if z0 is not None and z1 is not None and abs((z1 - z0) - 5.0) <= 0.6:
                verdict, note = "pass", f"Z {z0} -> {z1}"
        elif sid in ("home", "load") and not st.get("has_error"):
            verdict, note = "needs_eye_check", "executed with no printer error; confirm by eye, then `selftest hardware --record`"
        if verdict != "fail":
            break
        time.sleep(2)
    if verdict == "fail" and not note:
        note = f"state did not match within the verify window (last: {st.get('state')}, nozzle {st.get('nozzle')})"
    return record(sid, verdict, note, {"request_id": req.get("request_id"), "result": req.get("result")})


def cmd_selftest(a, out):
    if a.selftest_cmd != "hardware":
        return
    if a.record:
        out["recorded"] = record(a.record, a.result or "pass", a.note or "")
    elif a.run:
        run_step(a.run, a, out)
    out["checklist"] = checklist()
    todo = [c for c in out["checklist"] if not c["verified"]]
    out["unverified"] = [c["step"] for c in todo]
    out["unattended_ok_next"] = [c["step"] for c in todo if c["ok_unattended"]]
    out["needs_someone_at_printer"] = [c["step"] for c in todo if not c["ok_unattended"]]
    out["how"] = ("`selftest hardware --run <step>` (gated steps wait for the owner's yes), then the result is recorded "
                  "in docs/PROTOCOL.md; `--record <step> --result pass|fail --note ...` for what you saw")


def register(sp, handlers: dict) -> None:
    x = sp.add_parser("selftest", help="hardware self-test checklist (gated, one step at a time)")
    s = x.add_subparsers(dest="selftest_cmd", required=True)
    y = s.add_parser("hardware")
    y.add_argument("--run", help=f"step: {', '.join(STEPS)}")
    y.add_argument("--record", help="record a result for a step by hand")
    y.add_argument("--result", choices=("pass", "fail", "skip", "needs_eye_check"))
    y.add_argument("--note")
    y.add_argument("--file", help="upload step: small .gcode")
    y.add_argument("--file-on-printer", dest="file_on_printer", help="start step")
    y.add_argument("--nozzle", type=float, help="heat step")
    y.add_argument("--pct", type=float, help="fan step")
    y.add_argument("--z", type=float, help="move step (relative mm, default 5)")
    y.add_argument("--material", help="load step")
    y.add_argument("--printer", dest="printer_name", default=argparse.SUPPRESS)
    handlers["selftest"] = cmd_selftest
