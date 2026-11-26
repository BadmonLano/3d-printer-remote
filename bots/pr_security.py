"""API tokens and roles for the service API (part of the single printer_remote CLI).

Tokens are shown once, stored only as salted PBKDF2-SHA256 hashes in bots/state/tokens.json (gitignored).
Roles:  viewer   read fleet / queue / history / approvals list / camera
        operator viewer + add farm jobs, create gated requests (they still wait for the owner), kill switch on
        owner    everything: approve/decline requests, clear kill switches, change policy, manage tokens
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

import printer_remote as core

ROLES = ("viewer", "operator", "owner")
PERMS = {
    "viewer": {"read"},
    "operator": {"read", "queue", "request", "kill"},
    "owner": {"read", "queue", "request", "kill", "approve", "unkill", "policy", "tokens"},
}
ITER = 120_000


def _path():
    return core.state_dir() / "tokens.json"


def load_tokens() -> list[dict]:
    return core._read_json(_path()) or []


def _hash(token: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(salt), ITER).hex()


def create_token(name: str, role: str) -> tuple[str, dict]:
    if role not in ROLES:
        raise core.Fail(f"role must be one of {ROLES}")
    name = core.safe_name(name)
    rows = load_tokens()
    if any(r["name"] == name and not r.get("revoked") for r in rows):
        raise core.Fail(f"an active token named {name} exists; revoke it first")
    token = "trm_" + secrets.token_urlsafe(32)
    salt = secrets.token_hex(16)
    rec = {"id": "t" + secrets.token_hex(4), "name": name, "role": role, "salt": salt, "hash": _hash(token, salt),
           "prefix": token[:8], "created_at": core.ts_human(), "revoked": False, "last_used": None}
    rows.append(rec)
    core.atomic_write(_path(), json.dumps(rows, indent=2))
    return token, rec


def verify(token: str | None) -> dict | None:
    if not token or not token.startswith("trm_"):
        return None
    rows = load_tokens()
    hit = None
    for r in rows:
        if r.get("revoked"):
            continue
        if hmac.compare_digest(_hash(token, r["salt"]), r["hash"]):
            hit = r
    if hit:
        now = time.time()
        if now - float(hit.get("last_used_epoch") or 0) > 60:  # throttle writes
            hit["last_used"], hit["last_used_epoch"] = core.ts_human(), now
            core.atomic_write(_path(), json.dumps(rows, indent=2))
    return hit


def allowed(role: str | None, perm: str) -> bool:
    return perm in PERMS.get(role or "", set())


def public(r: dict) -> dict:
    return {k: r.get(k) for k in ("id", "name", "role", "prefix", "created_at", "revoked", "last_used")}


def cmd_token(a, out):
    pol = core._mod("pr_policy")
    if core.current_actor().get("role") != "owner":
        raise core.Fail("token management needs the owner role")
    c = a.token_cmd
    if c == "create":
        tok, rec = create_token(a.name, a.role)
        out["token"] = tok
        out["record"] = public(rec)
        out["note"] = "shown once; only a hash is stored. Send it as `Authorization: Bearer <token>`."
        pol.audit("token_create", detail=f"{rec['name']} ({rec['role']})")
    elif c == "list":
        out["tokens"] = [public(r) for r in load_tokens()]
    elif c == "revoke":
        rows = load_tokens()
        hit = [r for r in rows if r["id"] == a.id or r["name"] == a.id]
        if not hit:
            raise core.Fail(f"no token {a.id}")
        for r in hit:
            r["revoked"] = True
            r["revoked_at"] = core.ts_human()
        core.atomic_write(_path(), json.dumps(rows, indent=2))
        out["revoked"] = [public(r) for r in hit]
        pol.audit("token_revoke", detail=a.id)


def register(sp, handlers: dict) -> None:
    x = sp.add_parser("token", help="owner: API tokens for the service (hashed at rest)")
    ts = x.add_subparsers(dest="token_cmd", required=True)
    y = ts.add_parser("create")
    y.add_argument("--name", required=True)
    y.add_argument("--role", required=True, choices=ROLES)
    ts.add_parser("list")
    y = ts.add_parser("revoke")
    y.add_argument("id", help="token id or name")
    handlers["token"] = cmd_token
