"""Shared farm queue and routing (part of the single printer_remote CLI).

Jobs route to printers by material, color, nozzle, bed fit, availability and priority, with dependencies,
N copies spread across the farm, batch affinity (same batch/color stays on one printer to avoid swaps), and failover
to another printer when a job fails. Every start still goes through request_gated, so the autonomy policy decides:
owner_yes (default: a normal approval request), auto_after_preflight (trusted profiles only), or notify_only.
State: bots/state/farm_queue.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

import printer_remote as core

OPEN = ("queued", "awaiting_approval", "printing")
TERMINAL = ("done", "failed", "cancelled")


def load_jobs() -> list[dict]:
    return core.load_store("farm_queue", [])


def save_jobs(jobs: list[dict]) -> None:
    core.save_store("farm_queue", jobs)


def _job(jobs, jid) -> dict:
    j = next((x for x in jobs if x["id"] == jid), None)
    if not j:
        raise core.Fail(f"no farm job {jid}")
    return j


def _event(j: dict, what: str, **kw) -> None:
    j.setdefault("events", []).append({"at": core.ts_human(), "event": what, **kw})
    j["updated_at"] = core.ts_human()


def _parse_size(s) -> list[float] | None:
    if not s:
        return None
    if isinstance(s, (list, tuple)):
        return [float(x) for x in s]
    parts = [float(x) for x in str(s).lower().replace("*", "x").split("x") if x.strip()]
    return parts if len(parts) in (2, 3) else None


def add_job(jobs: list[dict], file: str, *, name=None, material=None, color=None, nozzle=None, size=None, copies=1,
            priority=0, depends_on=None, batch=None, profile=None, order_id=None, max_attempts=2, note=None,
            due=None) -> list[dict]:
    if copies < 1 or copies > 500:
        raise core.Fail("copies must be 1-500")
    f = Path(file)
    est = {}
    if f.suffix.lower() == ".gcode" and f.is_file():
        try:
            ins = core.inspect_gcode(f, material)
            est = {"est_s": ins.get("estimated_s"), "est_g": ins.get("filament_g"), "est_cost": ins.get("cost_usd")}
            material = material or ins.get("material")
        except core.Fail:
            pass
    group = f"g{int(time.time() * 1000) % 100_000_000:08d}{len(jobs)}"
    made = []
    deps = [d for d in (depends_on or []) if d]
    known = {x["id"] for x in jobs}
    for d in deps:
        if d not in known:
            raise core.Fail(f"depends_on {d}: no such job")
    for i in range(copies):
        jid = f"fj{int(time.time() * 1000) % 10_000_000:07d}{len(jobs):03d}"
        j = {"id": jid, "name": (name or f.stem) + (f" #{i + 1}/{copies}" if copies > 1 else ""), "file": str(f),
             "material": (material or "").upper() or None, "color": (color or "").lower() or None,
             "nozzle_mm": float(nozzle) if nozzle else None, "size_mm": _parse_size(size), "priority": int(priority),
             "depends_on": deps, "batch": batch, "profile": profile, "order_id": order_id, "copy_group": group,
             "copy_index": i + 1, "copies": copies, "status": "queued", "printer": None, "attempts": 0,
             "max_attempts": int(max_attempts), "excluded_printers": [], "created_at": core.ts_human(),
             "created_epoch": time.time() + i * 1e-3, "note": note, "due": due, **est}
        _event(j, "queued")
        jobs.append(j)
        made.append(j)
    return made


# ---------------------------------------------------------------- routing
def _fits(job: dict, p: dict) -> tuple[bool, str]:
    if job.get("material") and p.get("material") and job["material"].upper() != str(p["material"]).upper():
        return False, f"material {p.get('material')} loaded, job needs {job['material']}"
    if job.get("material") and not p.get("material"):
        return False, "printer has no loaded material set (fleet set --material)"
    if job.get("color") and str(p.get("color") or "").lower() != job["color"]:
        return False, f"color {p.get('color')} loaded, job needs {job['color']}"
    if job.get("nozzle_mm") and abs(float(p.get("nozzle_mm") or 0.4) - job["nozzle_mm"]) > 1e-6:
        return False, f"nozzle {p.get('nozzle_mm')} mm, job needs {job['nozzle_mm']} mm"
    if job.get("size_mm"):
        bed = p.get("bed_mm") or [220, 220, 250]
        sz = job["size_mm"]
        flat_ok = all(sz[i] <= bed[i] for i in range(min(len(sz), len(bed))))
        rot_ok = len(sz) >= 2 and sz[1] <= bed[0] and sz[0] <= bed[1] and (len(sz) < 3 or sz[2] <= bed[2])
        if not (flat_ok or rot_ok):
            return False, f"part {sz} mm does not fit bed {bed} mm"
    if p["name"] in (job.get("excluded_printers") or []):
        return False, "failed on this printer before (failover)"
    return True, "fits"


def _deps_done(job, by_id) -> bool:
    return all(by_id.get(d, {}).get("status") == "done" for d in job.get("depends_on") or [])


def _last_batch(jobs, printer) -> str | None:
    mine = [j for j in jobs if j.get("printer") == printer and j["status"] in ("done", "printing")]
    return mine[-1].get("batch") if mine else None


def plan(jobs: list[dict], fleet_rows: list[dict]) -> dict:
    """Pure routing: one job per available printer. Returns assignments + why the rest wait."""
    by_id = {j["id"]: j for j in jobs}
    ready = [j for j in jobs if j["status"] == "queued" and _deps_done(j, by_id)]
    waiting_deps = [j["id"] for j in jobs if j["status"] == "queued" and not _deps_done(j, by_id)]
    for j in jobs:
        if j["status"] == "queued" and any(by_id.get(d, {}).get("status") in ("failed", "cancelled") for d in j.get("depends_on") or []):
            j["blocked_reason"] = "a dependency failed or was cancelled"
    avail = [p for p in fleet_rows if p.get("available")]
    # scarcity: a job only one printer can take goes first, so a flexible job does not steal its only printer
    reach = {j["id"]: sum(1 for p in avail if _fits(j, p)[0]) for j in ready}
    assignments, taken = [], set()
    for p in avail:
        lb = _last_batch(jobs, p["name"])
        cands = []
        for j in ready:
            if j["id"] in taken:
                continue
            ok, why = _fits(j, p)
            if ok:
                cands.append(j)
        if not cands:
            continue
        cands.sort(key=lambda j: (-j["priority"], reach.get(j["id"], 0), 0 if (lb and j.get("batch") == lb) else 1,
                                  j["created_epoch"]))
        best = cands[0]
        taken.add(best["id"])
        assignments.append({"job": best["id"], "name": best["name"], "printer": p["name"], "priority": best["priority"],
                            "batch_affinity": bool(lb and best.get("batch") == lb)})
    unrouted = []
    for j in ready:
        if j["id"] in taken:
            continue
        why = [f"{p['name']}: {_fits(j, p)[1] if p.get('available') else 'unavailable (' + ', '.join(p.get('unavailable_reasons') or []) + ')'}"
               for p in fleet_rows]
        if not any(_fits(j, p)[0] for p in fleet_rows):
            why.insert(0, "NO printer in the fleet can take this job as configured")
        unrouted.append({"job": j["id"], "name": j["name"], "why": why})
    return {"assignments": assignments, "waiting_on_dependencies": waiting_deps, "unrouted": unrouted}


# ---------------------------------------------------------------- dispatch (goes through policy + approval)
def dispatch(jobs: list[dict], fleet_rows: list[dict], out: dict, dry_run: bool = False) -> list[dict]:
    pl = plan(jobs, fleet_rows)
    results = []
    if dry_run:
        return [{"dry_run": True, **a} for a in pl["assignments"]]
    for asg in pl["assignments"]:
        j = _job(jobs, asg["job"])
        prow = next(p for p in fleet_rows if p["name"] == asg["printer"])
        ctx = None if prow.get("implicit") else prow["name"]
        prev = core.active_printer()
        core.set_active_printer(ctx)
        sub = {"status": "ok", "warnings": [], "errors": [], "job_id": out.get("job_id")}
        rec = {"job": j["id"], "printer": prow["name"]}
        try:
            fname = core.safe_name(Path(j["file"]).stem + f"_{j['id']}.gcode")
            if Path(j["file"]).is_file():
                if not j["file"].lower().endswith(".gcode"):
                    raise core.Fail(f"{j['file']}: farm jobs need sliced .gcode (or a file already on the printer)")
                up = core.upload_gcode(core.printer_cfg(), Path(j["file"]), fname)
                rec["upload"] = {k: up.get(k) for k in ("uploaded", "http_status", "mb")}
            else:
                fname = Path(j["file"]).name  # assume it already sits on the printer
            ns = argparse.Namespace(file_on_printer=fname, gcode_root=None, color=j.get("color"), material=j.get("material"))
            j["status"], j["printer"] = "awaiting_approval", prow["name"]
            save_jobs(jobs)
            core.request_gated("start", ns, sub, reason=f"Farm job {j['name']} ({j['id']}) on {prow['name']}",
                               extra={"_params": {"farm_job_id": j["id"], "profile": j.get("profile"),
                                                  "local_gcode": j["file"] if Path(j["file"]).is_file() else None,
                                                  "color": j.get("color"), "material": j.get("material")}})
            jobs[:] = load_jobs()  # the approval hook may have updated it already (auto policy)
            j = _job(jobs, asg["job"])
            rec.update(status=sub["status"], request_id=sub.get("request_id"), errors=sub["errors"])
            if sub["status"] == "awaiting_owner":
                j["status"], j["request_id"] = "awaiting_approval", sub.get("request_id")
                _event(j, "approval_requested", printer=prow["name"], request_id=sub.get("request_id"))
            elif sub["status"] == "notified":
                j["status"], j["printer"] = "queued", None
                j["notified_at"] = core.ts_human()
                _event(j, "notify_only", printer=prow["name"])
            elif sub["status"] == "ok" and sub.get("auto"):
                pass  # on_request_outcome marked it printing
            elif j["status"] != "printing":
                j["status"], j["printer"] = "queued", None
                _event(j, "dispatch_failed", printer=prow["name"], errors=sub["errors"][:2])
        except Exception as e:
            jobs[:] = load_jobs()
            j = _job(jobs, asg["job"])
            j["status"], j["printer"] = "queued", None
            _event(j, "dispatch_failed", printer=prow["name"], error=str(e)[:160])
            rec.update(status="error", errors=[str(e)[:200]])
        finally:
            core.set_active_printer(prev)
        save_jobs(jobs)
        results.append(rec)
    return results


def on_request_outcome(req: dict) -> None:
    """Hook from approval: keep the farm job in step with its start request."""
    jid = (req.get("params") or {}).get("farm_job_id")
    if not jid or req.get("action") != "start":
        return
    jobs = load_jobs()
    j = next((x for x in jobs if x["id"] == jid), None)
    if not j:
        return
    oc = req.get("outcome")
    if oc == "executed":
        j["status"] = "printing"
        j["attempts"] = j.get("attempts", 0) + 1
        j["started_epoch"] = time.time()
        j["history_id"] = (req.get("result") or {}).get("history_id")
        _event(j, "started", printer=j.get("printer"), approved_by=(req.get("answer") or {}).get("approved_by"))
    elif oc in ("declined", "expired", "preflight_failed", "rate_limited"):
        _event(j, oc, printer=j.get("printer"))
        j["status"], j["printer"], j["request_id"] = "queued", None, None
        if oc == "declined":
            j["status"] = "held"  # owner said no: hold until `farm release`
    save_jobs(jobs)


def mark_finished(jobs: list[dict], jid: str, ok: bool, reason: str = "", failover: bool = True) -> dict:
    j = _job(jobs, jid)
    printer = j.get("printer")
    if ok:
        j["status"] = "done"
        j["ended_epoch"] = time.time()
        _event(j, "done", printer=printer)
        return {"job": jid, "status": "done"}
    _event(j, "failed", printer=printer, reason=reason)
    if failover and j.get("attempts", 0) < j.get("max_attempts", 2):
        if printer and printer not in j["excluded_printers"]:
            j["excluded_printers"].append(printer)
        j["status"], j["printer"], j["request_id"] = "queued", None, None
        _event(j, "requeued_failover", excluded=j["excluded_printers"])
        return {"job": jid, "status": "requeued", "excluded_printers": j["excluded_printers"]}
    j["status"] = "failed"
    j["ended_epoch"] = time.time()
    return {"job": jid, "status": "failed"}


def sync(jobs: list[dict], fleet_rows: list[dict]) -> list[dict]:
    """Move printing jobs to done/failed from live printer state. Failover requeues failures (start still gated)."""
    changes = []
    by_name = {p["name"]: p for p in fleet_rows}
    for j in [x for x in jobs if x["status"] == "printing"]:
        p = by_name.get(j.get("printer"))
        if not p or not p.get("reachable"):
            continue
        st = str(p.get("state"))
        if p.get("printing") and not p.get("idle"):
            if p.get("has_error"):
                changes.append(mark_finished(jobs, j["id"], False, f"printer error {p.get('error')}"))
            continue
        if time.time() - j.get("started_epoch", 0) < float(core.env("FARM_START_GRACE_S") or 90):
            continue  # printer may not report printing for a moment after start
        if st == "complete" or (p.get("progress_pct") or 0) >= 99:
            changes.append(mark_finished(jobs, j["id"], True))
        else:
            changes.append(mark_finished(jobs, j["id"], False, f"printer went {st} at {p.get('progress_pct')}%"))
    return changes


def intake(path: Path, jobs: list[dict]) -> list[dict]:
    """Order file -> farm jobs. CSV columns or JSON keys: order_id, file, material, color, copies, priority, nozzle,
    size (XxYxZ), batch, profile, due, note, depends_on (job ids, ;-separated)."""
    if not path.is_file():
        raise core.Fail(f"order file not found: {path}")
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows = rows.get("orders", rows) if isinstance(rows, dict) else rows
    else:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    made, base = [], path.parent
    seen = {j.get("order_id") + "|" + j["file"] for j in jobs if j.get("order_id")}
    for i, r in enumerate(rows):
        r = {str(k).strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
        if not r.get("file"):
            raise core.Fail(f"row {i + 1}: file is required")
        fp = Path(r["file"])
        if not fp.is_absolute() and (base / fp).exists():
            fp = base / fp
        key = f"{r.get('order_id')}|{fp}"
        if r.get("order_id") and key in seen:
            continue  # idempotent: re-importing the same order file does not duplicate jobs
        deps = r.get("depends_on") or []
        if isinstance(deps, str):
            deps = [d for d in deps.split(";") if d]
        made += add_job(jobs, str(fp), material=r.get("material"), color=r.get("color"), nozzle=r.get("nozzle") or None,
                        size=r.get("size"), copies=int(r.get("copies") or 1), priority=int(r.get("priority") or 0),
                        depends_on=deps, batch=r.get("batch") or r.get("order_id"), profile=r.get("profile") or None,
                        order_id=r.get("order_id"), note=r.get("note"), due=r.get("due"), name=r.get("name"))
    return made


# ---------------------------------------------------------------- CLI
def _fleet_rows(a=None):
    return core._mod("pr_fleet").fleet_status(wait=getattr(a, "wait", 3.0) if a else 3.0)


def cmd_farm(a, out):
    c = a.farm_cmd
    jobs = load_jobs()
    pol = core._mod("pr_policy")
    if c == "add":
        made = add_job(jobs, a.file, name=a.name, material=a.material, color=a.color, nozzle=a.nozzle, size=a.size,
                       copies=a.copies, priority=a.priority, depends_on=(a.depends or "").split(",") if a.depends else [],
                       batch=a.batch, profile=a.profile, max_attempts=a.max_attempts, note=a.note)
        save_jobs(jobs)
        out["added"] = made
        pol.audit("farm_add", detail=f"{len(made)} x {a.file}")
    elif c == "list":
        out["jobs"] = [j for j in jobs if a.all or j["status"] not in TERMINAL]
        out["counts"] = {s: sum(1 for j in jobs if j["status"] == s) for s in ("queued", "held", "awaiting_approval", "printing", "done", "failed", "cancelled")}
    elif c == "plan":
        out.update(plan(jobs, _fleet_rows(a)))
        out["note"] = "dry run: nothing uploaded or requested"
    elif c == "dispatch":
        rows = _fleet_rows(a)
        if a.printer_name:
            rows = [r for r in rows if r["name"] == a.printer_name]
        out["dispatched"] = dispatch(jobs, rows, out, dry_run=a.dry_run)
        if not out["dispatched"]:
            out["note"] = "nothing to dispatch (no available printer fits a ready job); see `farm plan`"
    elif c == "sync":
        rows = _fleet_rows(a)
        out["changes"] = sync(jobs, rows)
        save_jobs(jobs)
        for ch in out["changes"]:
            ev = "done" if ch["status"] == "done" else "failed"
            core._mod("pr_alerts").emit(ev, f"Farm job {ch['job']} {ch['status']}", ch, printer=None)
    elif c in ("done", "fail"):
        out["result"] = mark_finished(jobs, a.id, c == "done", a.reason or "", failover=not a.no_failover)
        save_jobs(jobs)
        pol.audit(f"farm_{c}", detail=f"{a.id} {a.reason or ''}")
    elif c == "cancel":
        j = _job(jobs, a.id)
        if j["status"] == "printing":
            raise core.Fail("job is printing; cancel it on the printer (`printer cancel --printer ...`, gated) then `farm fail`")
        j["status"] = "cancelled"
        _event(j, "cancelled")
        save_jobs(jobs)
        pol.audit("farm_cancel", detail=a.id)
    elif c == "release":
        j = _job(jobs, a.id)
        if j["status"] not in ("held",):
            raise core.Fail(f"job is {j['status']}, not held")
        j["status"] = "queued"
        _event(j, "released")
        save_jobs(jobs)
    elif c == "priority":
        j = _job(jobs, a.id)
        j["priority"] = a.value
        _event(j, "priority", value=a.value)
        save_jobs(jobs)
        out["job"] = j
    elif c == "intake":
        made = intake(Path(a.order_file), jobs)
        save_jobs(jobs)
        out["added"] = [{"id": j["id"], "name": j["name"], "order_id": j.get("order_id")} for j in made]
        out["count"] = len(made)
        pol.audit("farm_intake", detail=f"{len(made)} jobs from {Path(a.order_file).name}")


def register(sp, handlers: dict) -> None:
    x = sp.add_parser("farm", help="shared fleet queue: add, intake orders, plan, dispatch (policy-gated), sync, failover")
    fs = x.add_subparsers(dest="farm_cmd", required=True)
    y = fs.add_parser("add", help="add a sliced .gcode (or a file already on the printers) as farm job(s)")
    y.add_argument("file")
    y.add_argument("--name")
    y.add_argument("--material")
    y.add_argument("--color")
    y.add_argument("--nozzle", type=float)
    y.add_argument("--size", help="part footprint XxYxZ mm for bed-fit routing")
    y.add_argument("--copies", type=int, default=1, help="N copies spread across the farm")
    y.add_argument("--priority", type=int, default=0, help="higher runs first")
    y.add_argument("--depends", help="comma list of job ids that must be done first")
    y.add_argument("--batch", help="batch key: same-batch jobs prefer the same printer")
    y.add_argument("--profile", help="policy profile (e.g. a trusted auto-start profile)")
    y.add_argument("--max-attempts", dest="max_attempts", type=int, default=2, help="tries before failover gives up")
    y.add_argument("--note")
    y = fs.add_parser("list")
    y.add_argument("--all", action="store_true")
    for n in ("plan", "dispatch", "sync"):
        y = fs.add_parser(n)
        y.add_argument("--wait", type=float, default=3.0)
        if n == "dispatch":
            y.add_argument("--printer", dest="printer_name", help="only this printer")
            y.add_argument("--dry-run", dest="dry_run", action="store_true")
    for n in ("done", "fail"):
        y = fs.add_parser(n)
        y.add_argument("id")
        y.add_argument("--reason")
        y.add_argument("--no-failover", dest="no_failover", action="store_true")
    for n in ("cancel", "release"):
        y = fs.add_parser(n)
        y.add_argument("id")
    y = fs.add_parser("priority")
    y.add_argument("id")
    y.add_argument("value", type=int)
    y = fs.add_parser("intake", help="CSV/JSON order file -> farm jobs (idempotent per order_id+file)")
    y.add_argument("order_file")
    handlers["farm"] = cmd_farm
