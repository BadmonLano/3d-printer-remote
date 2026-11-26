"""Farm operations: machine-hour maintenance per printer, spool locations, analytics (part of the printer_remote CLI).

- Maintenance: the 1.x task list (MAINT_TASKS) per printer, with per-printer overrides in printers.json
  ("maintenance": {"nozzle_replace": 300}). Machine hours come from print history (per printer) plus manual hours.
  MAINT_BLOCK_DISPATCH=true makes due maintenance block farm dispatch for that printer (default false: alert only).
- Spools: the existing filament inventory gains a `location` ("shelf:<name>" or "printer:<name>"). Moving a spool onto a
  fleet printer also sets that printer's loaded material/color, which is what farm routing reads.
- Analytics: utilization, success rate, machine hours, filament, cost and cost per part, per printer and overall.
"""
from __future__ import annotations

import time

import printer_remote as core


# ---------------------------------------------------------------- machine hours + maintenance
def machine_hours(printer: str | None) -> dict:
    """printer None/'default' = classic single printer (its 1.x maintenance counter)."""
    if not printer or printer == "default":
        m = core.load_store("maintenance", {})
        return {"hours": round(float(m.get("print_hours", 0)), 3), "prints": int(m.get("prints", 0))}
    hist = [h for h in core.load_store("history", []) if h.get("printer") == printer
            and h.get("result") in ("success", "failed", "cancelled")]
    fm = core.load_store("fleet_maint", {}).get(printer, {})
    hrs = sum((h.get("duration_s") or 0) for h in hist) / 3600 + float(fm.get("extra_hours", 0))
    return {"hours": round(hrs, 3), "prints": len(hist)}


def schedule_for(printer: str | None) -> dict:
    sched = {k: v for k, v in core.MAINT_TASKS.items()}
    if printer and printer != "default":
        p = core._mod("pr_fleet").get_printer(printer) or {}
        for task, every in (p.get("maintenance") or {}).items():
            if isinstance(every, (int, float)):
                sched[task] = (float(every), None)
            elif isinstance(every, dict):
                sched[task] = (every.get("hours"), every.get("prints"))
    return sched


def maint_for(printer: str | None) -> dict:
    mh = machine_hours(printer)
    if not printer or printer == "default":
        last_all = core.load_store("maintenance", {}).get("last_done") or {}
    else:
        last_all = core.load_store("fleet_maint", {}).get(printer, {}).get("last_done") or {}
    tasks = []
    for task, (h_every, p_every) in schedule_for(printer).items():
        last = last_all.get(task, {"hours": 0, "prints": 0})
        hs, ps = mh["hours"] - float(last.get("hours", 0)), mh["prints"] - int(last.get("prints", 0))
        due = bool((h_every and hs >= h_every) or (p_every and ps >= p_every))
        soon = bool((h_every and hs >= 0.8 * h_every) or (p_every and ps >= 0.8 * p_every))
        tasks.append({"task": task, "status": "due" if due else ("soon" if soon else "ok"), "hours_since": round(hs, 1),
                      "prints_since": ps, "every_hours": h_every, "every_prints": p_every, "last_done": last.get("at")})
    return {"printer": printer or "default", "machine_hours": mh["hours"], "prints": mh["prints"], "tasks": tasks,
            "due": [t["task"] for t in tasks if t["status"] == "due"]}


def maint_due_blocking(printer: str) -> list[str]:
    if not core.env_bool("MAINT_BLOCK_DISPATCH", False):
        return []
    return maint_for(printer)["due"]


def maint_done(printer: str | None, task: str, note: str | None) -> dict:
    if task not in schedule_for(printer):
        raise core.Fail(f"unknown task; choose: {', '.join(schedule_for(printer))}")
    mh = machine_hours(printer)
    rec = {"hours": mh["hours"], "prints": mh["prints"], "at": core.ts_human(), "note": note}
    if not printer or printer == "default":
        m = core.load_store("maintenance", {})
        m.setdefault("last_done", {})[task] = rec
        m.setdefault("log", []).append({"at": rec["at"], "task": task, "note": note})
        core.save_store("maintenance", m)
    else:
        fm = core.load_store("fleet_maint", {})
        e = fm.setdefault(printer, {})
        e.setdefault("last_done", {})[task] = rec
        e.setdefault("log", []).append({"at": rec["at"], "task": task, "note": note})
        core.save_store("fleet_maint", fm)
    core._mod("pr_policy").audit("maint_done", printer=printer, detail=f"{task} {note or ''}")
    return maint_for(printer)


# ---------------------------------------------------------------- spools with location
def spools() -> list[dict]:
    inv = core.load_store("inventory", [])
    for s in inv:
        s.setdefault("location", "shelf:unassigned")
    return inv


def spool_move(sid: str, to: str) -> dict:
    if not (to.startswith("shelf:") or to.startswith("printer:")):
        raise core.Fail("--to must be shelf:<name> or printer:<name>")
    inv = spools()
    sp = next((s for s in inv if s["id"] == sid), None)
    if not sp:
        raise core.Fail(f"no spool {sid}")
    res = {"spool": sp, "previous_location": sp.get("location")}
    if to.startswith("printer:"):
        pn = to.split(":", 1)[1]
        fl = core._mod("pr_fleet")
        rows = fl.load_fleet()
        p = next((r for r in rows if r["name"] == pn), None)
        if rows and not p:
            raise core.Fail(f"no fleet printer {pn}")
        for other in inv:  # one spool per printer: the old one goes back to the shelf
            if other is not sp and other.get("location") == to:
                other["location"] = "shelf:unassigned"
                res.setdefault("unloaded", []).append(other["id"])
        if p:
            p["material"], p["color"] = sp.get("material"), sp.get("color")
            fl.save_fleet(rows)
            res["printer_loaded"] = {"printer": pn, "material": p["material"], "color": p["color"]}
    sp["location"] = to
    sp["moved_at"] = core.ts_human()
    core.save_store("inventory", inv)
    core._mod("pr_policy").audit("spool_move", detail=f"{sid} -> {to}")
    return res


def low_spools() -> list[dict]:
    low = core.env_float("FILAMENT_LOW_G", 150)
    return [s for s in spools() if not s.get("archived") and float(s.get("grams_left", 0)) < low]


# ---------------------------------------------------------------- analytics
def analytics(days: float) -> dict:
    now = time.time()
    since = now - days * 86400
    hist = [h for h in core.load_store("history", []) if float(h.get("started_epoch") or 0) >= since]
    rows = core._mod("pr_fleet").printers()
    names = [r["name"] for r in rows]
    for h in hist:
        n = h.get("printer") or "default"
        if n not in names:
            names.append(n)
    per = []
    window_h = days * 24
    for n in names:
        hs = [h for h in hist if (h.get("printer") or "default") == n]
        fin = [h for h in hs if h.get("result") in ("success", "failed", "cancelled")]
        ok = [h for h in fin if h["result"] == "success"]
        print_h = sum((h.get("duration_s") or 0) for h in fin) / 3600
        live = [h for h in hs if h.get("result") == "printing"]
        print_h += sum(max(0, now - float(h.get("started_epoch") or now)) for h in live) / 3600
        cost = sum(h.get("est_cost") or 0 for h in ok)
        per.append({"printer": n, "jobs": len(hs), "finished": len(fin), "success": len(ok),
                    "success_rate_pct": round(100 * len(ok) / len(fin), 1) if fin else None,
                    "print_hours": round(print_h, 2), "utilization_pct": round(100 * print_h / window_h, 1) if window_h else None,
                    "filament_g": round(sum(h.get("est_g") or 0 for h in ok), 1), "cost_usd": round(cost, 2),
                    "cost_per_part_usd": round(cost / len(ok), 2) if ok else None,
                    "machine_hours_total": machine_hours(None if n == "default" else n)["hours"]})
    jobs = core._mod("pr_farm").load_jobs()
    fj = [j for j in jobs if float(j.get("created_epoch") or 0) >= since]
    fin_all = sum(p["finished"] for p in per)
    ok_all = sum(p["success"] for p in per)
    cost_all = sum(p["cost_usd"] for p in per)
    return {"window_days": days, "printers": per,
            "overall": {"jobs": sum(p["jobs"] for p in per), "success_rate_pct": round(100 * ok_all / fin_all, 1) if fin_all else None,
                        "print_hours": round(sum(p["print_hours"] for p in per), 2),
                        "utilization_pct": round(sum(p["print_hours"] for p in per) / (window_h * max(1, len(per))) * 100, 1),
                        "cost_usd": round(cost_all, 2), "cost_per_part_usd": round(cost_all / ok_all, 2) if ok_all else None},
            "farm": {"jobs": len(fj), "done": sum(1 for j in fj if j["status"] == "done"),
                     "failed": sum(1 for j in fj if j["status"] == "failed"),
                     "failovers": sum(1 for j in fj for e in j.get("events", []) if e.get("event") == "requeued_failover"),
                     "queued": sum(1 for j in fj if j["status"] == "queued")}}


# ---------------------------------------------------------------- CLI
def cmd_ops(a, out):
    c = a.ops_cmd
    if c == "maint":
        names = [a.printer_name] if a.printer_name else [r["name"] for r in core._mod("pr_fleet").printers()]
        if a.done:
            out["maintenance"] = [maint_done(None if names[0] == "default" else names[0], a.done, a.note)]
            return
        if a.add_hours:
            if not a.printer_name or a.printer_name == "default":
                m = core.load_store("maintenance", {})
                m["print_hours"] = round(m.get("print_hours", 0) + a.add_hours, 3)
                core.save_store("maintenance", m)
            else:
                fm = core.load_store("fleet_maint", {})
                fm.setdefault(a.printer_name, {})["extra_hours"] = round(float(fm.get(a.printer_name, {}).get("extra_hours", 0)) + a.add_hours, 3)
                core.save_store("fleet_maint", fm)
        out["maintenance"] = [maint_for(None if n == "default" else n) for n in names]
        out["due"] = {m["printer"]: m["due"] for m in out["maintenance"] if m["due"]}
    elif c == "spool":
        if a.move:
            out.update(spool_move(a.move, a.to or ""))
        rows = spools()
        if a.location:
            rows = [s for s in rows if s.get("location") == a.location]
        out["spools"] = [s for s in rows if a.all or not s.get("archived")]
        out["by_location"] = {}
        for s in out["spools"]:
            out["by_location"].setdefault(s.get("location"), []).append(s["id"])
        out["low"] = [s["id"] for s in low_spools()]
    elif c == "analytics":
        out["analytics"] = analytics(a.days)


def register(sp, handlers: dict) -> None:
    x = sp.add_parser("ops", help="farm operations: per-printer maintenance, spool locations, analytics")
    s = x.add_subparsers(dest="ops_cmd", required=True)
    y = s.add_parser("maint", help="machine-hour maintenance per printer")
    y.add_argument("--printer", dest="printer_name")
    y.add_argument("--done", help="mark a task done (needs --printer in a fleet)")
    y.add_argument("--note")
    y.add_argument("--add-hours", dest="add_hours", type=float)
    y = s.add_parser("spool", help="spools with location; --move ID --to printer:<name>|shelf:<name>")
    y.add_argument("--move")
    y.add_argument("--to")
    y.add_argument("--location")
    y.add_argument("--all", action="store_true")
    y = s.add_parser("analytics")
    y.add_argument("--days", type=float, default=30)
    handlers["ops"] = cmd_ops
