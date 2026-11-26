"""Autonomy policies, kill switches, rate limits and the audit log (part of the single printer_remote CLI).

Default is unchanged from 1.x: every physical action needs the owner's yes, then preflight. A policy file is opt-in:
  policy.json (gitignored; see policy.example.json), or PR_POLICY_FILE.
Levels:
  owner_yes             owner says yes in chat -> preflight -> execute (default)
  auto_after_preflight  execute after preflight with no owner yes; only for AUTO_ELIGIBLE actions the policy lists
                        in `auto_actions`, only for trusted profiles/printers/groups, rate limited, fully audited
  notify_only           never sends the command; writes a notice packet so a human can do it by hand
Urgent cooldown is outside policy (unchanged): heaters off now, always allowed, always logged.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import printer_remote as core

LEVELS = ("owner_yes", "auto_after_preflight", "notify_only")
# Actions that may ever run without the owner. Heat, move, home, fan, gcode, filament moves and cancel never do.
AUTO_ELIGIBLE = ("start", "pause", "resume", "cooldown")
RESTRICTIVENESS = {"notify_only": 0, "owner_yes": 1, "auto_after_preflight": 2}  # lower = safer


# ---------------------------------------------------------------- policy file
def policy_path() -> Path:
    return Path(core.env("PR_POLICY_FILE") or (core.ROOT / "policy.json"))


def load_policy() -> dict:
    p = policy_path()
    j = core._read_json(p) if p.is_file() else None
    pol = j if isinstance(j, dict) else {}
    pol.setdefault("default", "owner_yes")
    pol.setdefault("printers", {})
    pol.setdefault("groups", {})
    pol.setdefault("profiles", {})
    pol.setdefault("auto_actions", ["start"])
    pol.setdefault("rate_limits", {"auto_per_hour": 6, "auto_per_printer_per_hour": 3})
    if pol["default"] not in LEVELS:
        pol["default"] = "owner_yes"
    return pol


def save_policy(pol: dict) -> None:
    core.atomic_write(policy_path(), json.dumps(pol, indent=2))


def _level_of(entry) -> str | None:
    if isinstance(entry, str):
        return entry if entry in LEVELS else None
    if isinstance(entry, dict):
        lv = entry.get("level")
        return lv if lv in LEVELS else None
    return None


def decide(action: str, params: dict, pol: dict | None = None) -> dict:
    """Effective level for one action. Order: profile > printer > group (safest of the printer's groups) > default.
    auto_after_preflight is downgraded to owner_yes unless the action is AUTO_ELIGIBLE and listed in auto_actions,
    and the profile's material/printer limits match."""
    pol = pol or load_policy()
    printer = params.get("printer") or core.active_printer()
    profile = params.get("profile")
    level, why = pol["default"], "default"
    groups = []
    if printer:
        try:
            groups = list((core._mod("pr_fleet").get_printer(printer) or {}).get("groups") or [])
        except Exception:
            groups = []
    gl = [(_level_of(pol["groups"].get(g)), g) for g in groups if _level_of(pol["groups"].get(g))]
    if gl:
        lv, g = min(gl, key=lambda x: RESTRICTIVENESS[x[0]])
        level, why = lv, f"group {g}"
    if printer and _level_of(pol["printers"].get(printer)):
        level, why = _level_of(pol["printers"][printer]), f"printer {printer}"
    pent = pol["profiles"].get(profile) if profile else None
    if pent is not None and _level_of(pent):
        plv = _level_of(pent)
        limits_ok, note = True, ""
        if isinstance(pent, dict):
            mats = [m.upper() for m in pent.get("materials") or []]
            if mats and str(params.get("material") or "").upper() not in mats:
                limits_ok, note = False, f"material {params.get('material')} not in profile {mats}"
            prs = pent.get("printers") or []
            if prs and printer not in prs:
                limits_ok, note = False, f"printer {printer} not in profile {prs}"
        # a profile can only grant auto where the printer/group is not locked to notify_only
        if level == "notify_only" and plv != "notify_only":
            why += " (locks notify_only; profile ignored)"
        elif limits_ok:
            level, why = plv, f"profile {profile}"
        else:
            why += f" (profile {profile} not applied: {note})"
    allowed_auto = [a for a in pol.get("auto_actions") or [] if a in AUTO_ELIGIBLE]
    if level == "auto_after_preflight" and action not in allowed_auto:
        level, why = "owner_yes", f"{why}; {action} is not auto-eligible (auto_actions={allowed_auto})"
    if level == "auto_after_preflight" and params.get("selftest_step"):
        level, why = "owner_yes", "hardware self-test steps always need the owner"
    return {"level": level, "why": why, "action": action, "printer": printer, "profile": profile}


# ---------------------------------------------------------------- kill switch
def _kill_path() -> Path:
    return core.state_dir() / "killswitch.json"


def kill_state() -> dict:
    return core._read_json(_kill_path()) or {"global": None, "printers": {}}


def is_killed(printer: str | None) -> tuple[bool, str | None]:
    k = kill_state()
    if k.get("global"):
        g = k["global"]
        return True, f"global kill by {g.get('by')} at {g.get('at')}: {g.get('reason')}"
    if printer and (k.get("printers") or {}).get(printer):
        e = k["printers"][printer]
        return True, f"printer {printer} killed by {e.get('by')} at {e.get('at')}: {e.get('reason')}"
    return False, None


def set_kill(printer: str | None, on: bool, reason: str | None) -> dict:
    k = kill_state()
    rec = {"at": core.ts_human(), "by": core.current_actor()["name"], "reason": (reason or "")[:200]} if on else None
    if printer:
        k.setdefault("printers", {})
        if on:
            k["printers"][printer] = rec
        else:
            k["printers"].pop(printer, None)
    else:
        k["global"] = rec
    core.atomic_write(_kill_path(), json.dumps(k, indent=2))
    audit("kill_on" if on else "kill_off", printer=printer, detail=reason)
    return k


# ---------------------------------------------------------------- audit log
def _audit_path() -> Path:
    return core.state_dir() / "audit.jsonl"


def audit(event: str, action: str | None = None, printer: str | None = None, request_id: str | None = None,
          detail=None, actor: dict | None = None) -> dict:
    a = actor or core.current_actor()
    rec = {"at": core.ts_human(), "epoch": round(time.time(), 3), "event": event, "actor": a.get("name"),
           "role": a.get("role"), "action": action, "printer": printer, "request_id": request_id,
           "detail": (str(detail)[:300] if detail is not None else None)}
    try:
        with open(_audit_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return rec


def read_audit(limit: int = 100, printer: str | None = None, actor: str | None = None, event: str | None = None,
               since_epoch: float | None = None) -> list[dict]:
    p = _audit_path()
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if printer and r.get("printer") != printer:
            continue
        if actor and r.get("actor") != actor:
            continue
        if event and r.get("event") != event:
            continue
        if since_epoch and r.get("epoch", 0) < since_epoch:
            continue
        rows.append(r)
    return rows[-limit:] if limit else rows


# ---------------------------------------------------------------- rate limits
def check_rate(printer: str | None, pol: dict | None = None) -> tuple[bool, str]:
    pol = pol or load_policy()
    rl = pol.get("rate_limits") or {}
    hour = time.time() - 3600
    done = read_audit(limit=0, event="auto_executed", since_epoch=hour)
    g = int(rl.get("auto_per_hour", 6))
    pp = int(rl.get("auto_per_printer_per_hour", 3))
    if len(done) >= g:
        return False, f"{len(done)} auto actions in the last hour (limit {g})"
    mine = [r for r in done if r.get("printer") == printer]
    if len(mine) >= pp:
        return False, f"{len(mine)} auto actions on {printer or 'default'} in the last hour (limit {pp})"
    return True, f"{len(done)}/{g} global, {len(mine)}/{pp} this printer in the last hour"


# ---------------------------------------------------------------- CLI
def _require_owner(what: str) -> None:
    if core.current_actor().get("role") != "owner":
        raise core.Fail(f"{what} needs the owner role")


def cmd_policy(a, out):
    c = a.policy_cmd
    if c == "show":
        out["policy_file"] = str(policy_path())
        out["policy_file_exists"] = policy_path().is_file()
        out["policy"] = load_policy()
        out["kill_switch"] = kill_state()
        out["levels"] = LEVELS
        out["auto_eligible_actions"] = AUTO_ELIGIBLE
    elif c == "check":
        params = {"printer": a.printer_name or core.active_printer(), "profile": a.profile, "material": a.material}
        out["decision"] = decide(a.action, params)
        out["killed"], out["kill_reason"] = is_killed(params["printer"])
        out["rate"] = dict(zip(("ok", "detail"), check_rate(params["printer"])))
    elif c == "set":
        _require_owner("changing the policy")
        if a.level not in LEVELS:
            raise core.Fail(f"level must be one of {LEVELS}")
        pol = load_policy()
        if a.profile:
            ent = pol["profiles"].get(a.profile) if isinstance(pol["profiles"].get(a.profile), dict) else {}
            ent["level"] = a.level
            if a.materials:
                ent["materials"] = [m.strip().upper() for m in a.materials.split(",") if m.strip()]
            pol["profiles"][a.profile] = ent
            target = f"profile {a.profile}"
        elif a.group:
            pol["groups"][a.group] = a.level
            target = f"group {a.group}"
        elif a.printer_name:
            pol["printers"][a.printer_name] = a.level
            target = f"printer {a.printer_name}"
        else:
            pol["default"] = a.level
            target = "default"
        if a.auto_actions:
            acts = [x.strip() for x in a.auto_actions.split(",") if x.strip()]
            bad = [x for x in acts if x not in AUTO_ELIGIBLE]
            if bad:
                raise core.Fail(f"never auto-eligible: {bad}; allowed: {AUTO_ELIGIBLE}")
            pol["auto_actions"] = acts
        save_policy(pol)
        audit("policy_set", printer=a.printer_name, detail=f"{target} -> {a.level}")
        out["policy"] = pol
        if a.level == "auto_after_preflight":
            out["warnings"].append("auto_after_preflight runs actions with nobody watching; keep a camera and the kill switch handy")
    elif c == "kill":
        out["kill_switch"] = set_kill(a.printer_name, True, a.reason or "manual kill")
        out["summary"] = ("all printers" if not a.printer_name else a.printer_name) + " blocked for physical actions " \
                         "(urgent cooldown still works)"
    elif c == "unkill":
        _require_owner("clearing a kill switch")
        out["kill_switch"] = set_kill(a.printer_name, False, a.reason)


def cmd_audit(a, out):
    out["audit"] = read_audit(a.limit, a.printer_name, a.actor_name, a.event)
    out["count"] = len(out["audit"])


def register(sp, handlers: dict) -> None:
    x = sp.add_parser("policy", help="autonomy levels per printer/group/profile, kill switch, rate limits")
    ps = x.add_subparsers(dest="policy_cmd", required=True)
    ps.add_parser("show")
    y = ps.add_parser("check", help="which level would apply to an action")
    y.add_argument("--action", required=True)
    y.add_argument("--printer", dest="printer_name")
    y.add_argument("--profile")
    y.add_argument("--material")
    y = ps.add_parser("set", help="owner: set a level for default / --printer / --group / --profile")
    y.add_argument("--level", required=True, choices=LEVELS)
    y.add_argument("--printer", dest="printer_name")
    y.add_argument("--group")
    y.add_argument("--profile")
    y.add_argument("--materials", help="profile only: comma list the profile is trusted for (e.g. PETG,PLA)")
    y.add_argument("--auto-actions", dest="auto_actions", help=f"comma list from {AUTO_ELIGIBLE}")
    y = ps.add_parser("kill", help="block physical actions (global, or --printer); urgent cooldown still works")
    y.add_argument("--printer", dest="printer_name")
    y.add_argument("--reason")
    y = ps.add_parser("unkill", help="owner: clear a kill switch")
    y.add_argument("--printer", dest="printer_name")
    y.add_argument("--reason")
    handlers["policy"] = cmd_policy
    x = sp.add_parser("audit", help="who did what: requests, approvals, auto runs, kills, API calls")
    x.add_argument("--limit", type=int, default=100)
    x.add_argument("--printer", dest="printer_name")
    x.add_argument("--by", dest="actor_name")
    x.add_argument("--event")
    handlers["audit"] = cmd_audit
