"""Service mode: `serve` runs a supervised daemon (fleet poller, farm sync/dispatch, optional camera watch) with a
pid file, a health endpoint, a local web dashboard and a JSON REST API (part of the single printer_remote CLI).

Safety: binds to 127.0.0.1 by default. Without tokens, loopback GETs get the viewer role (SERVE_LOCAL_TRUST=viewer);
every POST needs a token unless the owner opts in with SERVE_LOCAL_TRUST=owner on a single-user machine. A non-loopback
bind refuses to start until an owner or operator token exists. Starts still go through policy + approval.
Stop it with Ctrl+C, or `serve --stop` (writes a stop file the supervisor watches).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import printer_remote as core

LOCK = threading.RLock()  # serializes store writes made from the API and the workers inside one service
MAX_BODY = 65536


def _pid_path() -> Path:
    return core.state_dir() / "serve.pid"


def _stop_path() -> Path:
    return core.state_dir() / "serve.stop"


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


def _health_url(info: dict) -> str:
    h = info.get("host") or "127.0.0.1"
    h = "127.0.0.1" if h in ("0.0.0.0", "::", "") else h
    return f"http://{h}:{info.get('port')}/api/health"


def running_info() -> dict | None:
    info = core._read_json(_pid_path())
    if not info:
        return None
    try:
        with urllib.request.urlopen(_health_url(info), timeout=2) as r:
            j = json.loads(r.read() or b"{}")
            if j.get("pid") == info.get("pid"):
                return {**info, "health": j}
    except Exception:
        pass
    return None  # stale pid file


class Service:
    def __init__(self, host: str, port: int, interval: float, watch: bool, auto_dispatch: bool, duration: float | None):
        self.host, self.port, self.interval = host, port, interval
        self.watch, self.auto_dispatch, self.duration = watch, auto_dispatch, duration
        self.started = time.time()
        self.stop = threading.Event()
        self.workers: dict = {}
        self.fleet_cache: list = []
        self.fleet_at: float | None = None
        self.prev_status: dict = {}
        self.events: list = []
        self.httpd = None

    # ------------------------------------------------ supervision
    def _worker(self, name: str, fn, every: float):
        w = self.workers[name]
        while not self.stop.is_set():
            t0 = time.time()
            try:
                fn()
                w.update(last_ok=core.ts_human(), last_ok_epoch=time.time(), runs=w.get("runs", 0) + 1, last_error=None)
            except Exception as e:  # keep the loop alive; record and back off
                w.update(last_error=f"{type(e).__name__}: {str(e)[:200]}", errors=w.get("errors", 0) + 1,
                         last_error_at=core.ts_human())
                if w.get("crash_on_error"):
                    raise
            self.stop.wait(max(0.2, every - (time.time() - t0)))

    def _spawn(self, name: str, fn, every: float):
        self.workers.setdefault(name, {"restarts": -1, "every_s": every})
        self.workers[name]["restarts"] += 1
        t = threading.Thread(target=self._worker, args=(name, fn, every), name=f"trm-{name}", daemon=True)
        self.workers[name]["thread"] = t
        t.start()

    def supervise(self, specs: dict):
        for n, (fn, every) in specs.items():
            self._spawn(n, fn, every)
        end = time.time() + self.duration if self.duration else None
        backoff: dict = {}
        while not self.stop.is_set():
            if _stop_path().exists():
                try:
                    _stop_path().unlink()
                except OSError:
                    pass
                break
            if end and time.time() >= end:
                break
            for n, (fn, every) in specs.items():
                t = self.workers[n].get("thread")
                if t is not None and not t.is_alive() and not self.stop.is_set():
                    wait = backoff.get(n, 1.0)
                    time.sleep(min(wait, 0.2 if self.duration else wait))
                    backoff[n] = min(60.0, wait * 2)
                    self._spawn(n, fn, every)
            self.stop.wait(0.2)
        self.stop.set()

    # ------------------------------------------------ workers
    def poll(self):
        fl = core._mod("pr_fleet")
        alerts = core._mod("pr_alerts")
        rows = fl.fleet_status(wait=core.env_float("SERVE_STATUS_WAIT_S", 3))
        for r in rows:
            prev = self.prev_status.get(r["name"])
            ctx = None if r["name"] == "default" and not fl.load_fleet() else r["name"]
            if r.get("reachable"):
                evs = core.assess_continuity(prev, r, None)
                for e in evs:
                    if prev is None and e["kind"] != "power_loss_resume_pending":
                        continue
                    self.events.append({"at": core.ts_human(), "printer": r["name"], **e})
                    if e["kind"] in ("job_lost", "printer_error"):
                        core._mod("pr_plugins").run_playbook(e["kind"], ctx, e["detail"])
                    elif e["kind"] == "power_loss_resume_pending":
                        alerts.emit("anomaly", e["detail"], e, printer=r["name"], key="plr")
                self.prev_status[r["name"]] = r
            try:
                due = core._mod("pr_ops").maint_for(ctx)["due"]
                if due:
                    alerts.emit("maintenance_due", f"{r['name']}: {', '.join(due)}", {"due": due}, printer=r["name"],
                                key=",".join(due))
            except Exception:
                pass
        low = core._mod("pr_ops").low_spools()
        if low:
            alerts.emit("filament_low", ", ".join(f"{s['id']} {s.get('color')} {s.get('material')} {s.get('grams_left')} g"
                                                  for s in low), {"spools": [s["id"] for s in low]},
                        key=",".join(s["id"] for s in low))
        with LOCK:
            farm = core._mod("pr_farm")
            jobs = farm.load_jobs()
            changes = farm.sync(jobs, rows)
            if changes:
                farm.save_jobs(jobs)
            for ch in changes:
                alerts.emit("done" if ch["status"] == "done" else "failed", f"farm job {ch['job']} {ch['status']}", ch,
                            key=ch["job"])
        self.fleet_cache, self.fleet_at = rows, time.time()
        self.events = self.events[-200:]

    def dispatch(self):
        if not self.auto_dispatch or not self.fleet_cache:
            return
        with LOCK:
            farm = core._mod("pr_farm")
            jobs = farm.load_jobs()
            # release jobs whose approval request expired unanswered
            pend = {p.stem for p in (core.state_dir() / "pending").glob("*.json")} if (core.state_dir() / "pending").is_dir() else set()
            for j in jobs:
                if j["status"] == "awaiting_approval" and j.get("request_id") and j["request_id"] not in pend:
                    j["status"], j["printer"], j["request_id"] = "queued", None, None
            farm.save_jobs(jobs)
            out = {"job_id": core.new_job_id(), "warnings": [], "errors": []}
            farm.dispatch(jobs, self.fleet_cache, out)

    def watch_all(self):
        fl = core._mod("pr_fleet")
        for p in fl.printers():
            ctx = None if p.get("implicit") else p["name"]
            core.set_active_printer(ctx)
            try:
                if not core.camera_enabled():
                    continue
                o = {"warnings": [], "errors": [], "job_id": core.new_job_id()}
                core.watch_tick(o)
            finally:
                core.set_active_printer(None)

    # ------------------------------------------------ http
    def health(self) -> dict:
        now = time.time()
        ws = {}
        for n, w in self.workers.items():
            t = w.get("thread")
            ws[n] = {k: v for k, v in w.items() if k != "thread"} | {"alive": bool(t and t.is_alive())}
        stale = self.fleet_at is not None and now - self.fleet_at > max(3 * self.interval, 60)
        ok = all(v["alive"] for v in ws.values()) and not stale
        return {"status": "ok" if ok else "degraded", "version": core.VERSION, "pid": os.getpid(),
                "uptime_s": int(now - self.started), "workers": ws, "fleet_age_s": int(now - self.fleet_at) if self.fleet_at else None,
                "bind": f"{self.host}:{self.port}", "kill_switch": core._mod("pr_policy").kill_state()}

    def make_handler(self):
        svc = self
        sec = core._mod("pr_security")
        pol = core._mod("pr_policy")

        class H(BaseHTTPRequestHandler):
            server_version = "TRM/" + core.VERSION

            def log_message(self, *a):
                pass

            def _send(self, code: int, body, ctype="application/json"):
                raw = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                if ctype.startswith("text/html"):
                    self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; "
                                     "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self) -> dict | None:
                h = self.headers.get("Authorization", "")
                tok = h[7:].strip() if h.lower().startswith("bearer ") else None
                if tok:
                    rec = sec.verify(tok)
                    return {"name": f"token:{rec['name']}", "role": rec["role"]} if rec else None
                peer_local = _loopback(self.client_address[0])
                trust = (core.env("SERVE_LOCAL_TRUST") or "viewer").lower()
                if peer_local and _loopback(svc.host) and trust in sec.ROLES:
                    return {"name": f"local:{trust}", "role": trust}
                return None

            def _discard_body(self) -> None:
                # Read any posted body before writing an error, otherwise Windows
                # clients can see WinError 10053 instead of the 401/403.
                if getattr(self, "_body_consumed", False):
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 0:
                        self.rfile.read(n)
                except Exception:
                    pass
                self._body_consumed = True

            def _need(self, perm: str) -> dict | None:
                who = self._auth()
                if not who:
                    self._discard_body()
                    self._send(401, {"error": "token required (Authorization: Bearer <token>)"})
                    return None
                if not sec.allowed(who["role"], perm):
                    self._discard_body()
                    self._send(403, {"error": f"role {who['role']} cannot {perm}"})
                    return None
                core.set_actor(who["name"], who["role"])
                return who

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                self._body_consumed = True
                if n > MAX_BODY:
                    raise core.Fail("body too large")
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    j = json.loads(raw or b"{}")
                except ValueError:
                    raise core.Fail("body must be JSON")
                if not isinstance(j, dict):
                    raise core.Fail("body must be a JSON object")
                return j

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                path = u.path.rstrip("/") or "/"
                try:
                    if path == "/api/health":
                        return self._send(200, svc.health())
                    if path == "/":
                        return self._send(200, DASHBOARD.encode(), "text/html; charset=utf-8")
                    perm = "queue" if path == "/api/audit" else "read"
                    if not self._need(perm):
                        return
                    if path == "/api/fleet":
                        rows = svc.fleet_cache or core._mod("pr_fleet").fleet_status()
                        return self._send(200, {"printers": rows, "age_s": int(time.time() - svc.fleet_at) if svc.fleet_at else None,
                                                "events": svc.events[-30:]})
                    if path == "/api/queue":
                        return self._send(200, {"farm": core._mod("pr_farm").load_jobs(), "classic_queue": core.load_store("queue", [])})
                    if path == "/api/history":
                        lim = int((q.get("limit") or ["50"])[0])
                        return self._send(200, {"history": core.load_store("history", [])[-lim:]})
                    if path == "/api/approvals":
                        rows = []
                        for p in sorted((core.state_dir() / "pending").glob("*.json")) if (core.state_dir() / "pending").is_dir() else []:
                            r = core._read_json(p) or {}
                            if time.time() < r.get("expires_epoch", 0):
                                rows.append({k: r.get(k) for k in ("request_id", "action", "params", "summary", "created_at")})
                        return self._send(200, {"pending": rows})
                    if path == "/api/audit":
                        return self._send(200, {"audit": pol.read_audit(int((q.get("limit") or ["100"])[0]))})
                    if path == "/api/analytics":
                        return self._send(200, core._mod("pr_ops").analytics(float((q.get("days") or ["30"])[0])))
                    if path == "/api/maintenance":
                        fl = core._mod("pr_fleet")
                        return self._send(200, {"maintenance": [core._mod("pr_ops").maint_for(None if p.get("implicit") else p["name"])
                                                                for p in fl.printers()]})
                    if path == "/api/policy":
                        return self._send(200, {"policy": pol.load_policy(), "kill_switch": pol.kill_state()})
                    if path.startswith("/api/camera/"):
                        name = urllib.parse.unquote(path.split("/")[-1]).removesuffix(".jpg")
                        core.set_active_printer(None if name == "default" else name)
                        try:
                            snap = core.take_snapshot("dashboard", (q.get("camera") or [None])[0])
                        finally:
                            core.set_active_printer(None)
                        return self._send(200, Path(snap["path"]).read_bytes(), "image/jpeg")
                    return self._send(404, {"error": "not found"})
                except core.Fail as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    return self._send(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})
                finally:
                    core.set_actor(None)

            def do_POST(self):
                path = urllib.parse.urlparse(self.path).path.rstrip("/")
                parts = path.split("/")
                try:
                    if path.startswith("/api/approvals/"):
                        who = self._need("approve")
                        if not who:
                            return
                        b = self._body()
                        if b.get("answer") not in ("yes", "no"):
                            raise core.Fail('answer must be "yes" or "no"')
                        out = {"status": "ok", "warnings": [], "errors": [], "job_id": core.new_job_id()}
                        with LOCK:
                            core._approve_request(parts[-1], b["answer"] == "yes", f"api:{who['name']}", b.get("quote"), out)
                        return self._send(200, out)
                    if path == "/api/queue":
                        who = self._need("queue")
                        if not who:
                            return
                        b = self._body()
                        with LOCK:
                            farm = core._mod("pr_farm")
                            jobs = farm.load_jobs()
                            made = farm.add_job(jobs, b.get("file") or "", name=b.get("name"), material=b.get("material"),
                                                color=b.get("color"), nozzle=b.get("nozzle"), size=b.get("size"),
                                                copies=int(b.get("copies") or 1), priority=int(b.get("priority") or 0),
                                                depends_on=b.get("depends_on") or [], batch=b.get("batch"), profile=b.get("profile"))
                            farm.save_jobs(jobs)
                        pol.audit("api_queue_add", detail=f"{len(made)} x {b.get('file')}")
                        return self._send(200, {"added": made})
                    if path == "/api/farm/dispatch":
                        if not self._need("queue"):
                            return
                        out = {"job_id": core.new_job_id(), "warnings": [], "errors": []}
                        with LOCK:
                            farm = core._mod("pr_farm")
                            jobs = farm.load_jobs()
                            res = farm.dispatch(jobs, svc.fleet_cache or core._mod("pr_fleet").fleet_status(), out)
                        return self._send(200, {"dispatched": res})
                    if path in ("/api/kill", "/api/unkill"):
                        if not self._need("kill" if path == "/api/kill" else "unkill"):
                            return
                        b = self._body()
                        k = pol.set_kill(b.get("printer"), path == "/api/kill", b.get("reason") or "via API")
                        return self._send(200, {"kill_switch": k})
                    if len(parts) == 5 and parts[1] == "api" and parts[2] == "printers":
                        name, action = urllib.parse.unquote(parts[3]), parts[4]
                        if action == "urgent-cooldown":
                            if not self._need("kill"):
                                return
                            b = self._body()
                            core.set_active_printer(None if name == "default" else name)
                            try:
                                return self._send(200, {"urgent_cooldown": core.urgent_cooldown(b.get("reason") or "", "api")})
                            finally:
                                core.set_active_printer(None)
                        if action not in core.GATED_ACTIONS:
                            raise core.Fail(f"action must be one of {core.GATED_ACTIONS} or urgent-cooldown")
                        if not self._need("request"):
                            return
                        b = self._body()
                        ns = argparse.Namespace(file_on_printer=b.get("file"), gcode_root=None, nozzle=b.get("nozzle"),
                                                bed=b.get("bed"), pct=b.get("pct"), x=b.get("x"), y=b.get("y"), z=b.get("z"),
                                                relative=bool(b.get("relative")), feed=b.get("feed") or 3000,
                                                color=b.get("color"), material=b.get("material"))
                        out = {"status": "ok", "warnings": [], "errors": [], "job_id": core.new_job_id()}
                        core.set_active_printer(None if name == "default" else name)
                        try:
                            with LOCK:
                                core.request_gated(action, ns, out, reason=f"API request by {core.current_actor()['name']}")
                        finally:
                            core.set_active_printer(None)
                        return self._send(200, out)
                    self._discard_body()
                    return self._send(404, {"error": "not found"})
                except core.Fail as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    return self._send(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})
                finally:
                    core.set_actor(None)
        return H

    # ------------------------------------------------ run
    def run(self) -> dict:
        sec = core._mod("pr_security")
        if not _loopback(self.host):
            strong = [t for t in sec.load_tokens() if not t.get("revoked") and t["role"] in ("owner", "operator")]
            if not strong:
                raise core.Fail(f"refusing to bind {self.host}: create an owner/operator token first (`token create`) and "
                                "keep the farm network isolated (see SECURITY.md)")
        other = running_info()
        if other:
            raise core.Fail(f"serve already running (pid {other['pid']} on {other['host']}:{other['port']})")
        self.httpd = ThreadingHTTPServer((self.host, self.port), self.make_handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        core.atomic_write(_pid_path(), json.dumps({"pid": os.getpid(), "host": self.host, "port": self.port,
                                                   "started_at": core.ts_human()}))
        if _stop_path().exists():
            _stop_path().unlink()
        threading.Thread(target=self.httpd.serve_forever, name="trm-http", daemon=True).start()
        core._mod("pr_policy").audit("serve_start", detail=f"{self.host}:{self.port}")
        specs = {"poller": (self.poll, self.interval), "dispatcher": (self.dispatch, self.interval)}
        if self.watch:
            specs["watcher"] = (self.watch_all, core.env_float("WATCH_INTERVAL_S", 120))
        try:
            self.supervise(specs)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            self.httpd.shutdown()
            self.httpd.server_close()
            try:
                if (core._read_json(_pid_path()) or {}).get("pid") == os.getpid():
                    _pid_path().unlink()
            except OSError:
                pass
            core._mod("pr_policy").audit("serve_stop", detail=f"{self.host}:{self.port}")
        h = self.health()
        return {"ran_s": h["uptime_s"], "workers": h["workers"], "bind": h["bind"]}


def cmd_serve(a, out):
    if a.status:
        info = running_info()
        out["running"] = bool(info)
        out["info"] = info
        return
    if a.stop:
        info = running_info()
        if not info:
            out["running"] = False
            out["note"] = "not running"
            return
        core.atomic_write(_stop_path(), core.ts_human())
        out["stop_requested"] = True
        return
    svc = Service(a.host, a.port, a.interval, a.watch, a.auto_dispatch, a.duration)
    out["serve"] = svc.run()


def register(sp, handlers: dict) -> None:
    x = sp.add_parser("serve", help="daemon: supervised fleet poller + farm sync/dispatch + dashboard + REST API")
    x.add_argument("--host", default=core.env("SERVE_HOST") or "127.0.0.1")
    x.add_argument("--port", type=int, default=int(core.env("SERVE_PORT") or 8787))
    x.add_argument("--interval", type=float, default=float(core.env("SERVE_INTERVAL_S") or 30))
    x.add_argument("--watch", action="store_true", help="also run the camera malfunction watch for printers with cameras")
    x.add_argument("--auto-dispatch", dest="auto_dispatch", action="store_true",
                   help="route queued farm jobs to free printers each tick (starts still follow policy/approval)")
    x.add_argument("--duration", type=float, help="stop after N seconds (tests, soak runs)")
    x.add_argument("--status", action="store_true")
    x.add_argument("--stop", action="store_true")
    handlers["serve"] = cmd_serve


DASHBOARD = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>3D Printer Remote</title><style>
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
header{padding:12px 18px;background:#171a21;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0 12px 0 0} h2{font-size:15px;margin:18px 0 8px;color:#9fb3c8}
main{padding:0 18px 30px} .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{background:#171a21;border:1px solid #262b36;border-radius:8px;padding:12px}
.ok{color:#5fd38d}.bad{color:#ff6b6b}.warn{color:#ffc857}.mut{color:#8a94a6;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px} td,th{padding:5px 6px;border-bottom:1px solid #262b36;text-align:left}
button{background:#2b6cb0;color:#fff;border:0;border-radius:5px;padding:5px 10px;cursor:pointer} button.no{background:#7a2e2e}
input{background:#0f1115;color:#e6e6e6;border:1px solid #333;border-radius:5px;padding:5px}
img{max-width:100%;border-radius:6px;margin-top:6px}
</style></head><body><header><h1>3D Printer Remote</h1><span id="health" class="mut">...</span>
<span class="mut">token</span><input id="tok" type="password" size="28" placeholder="optional on localhost (viewer)">
<button onclick="saveTok()">save</button><button class="no" onclick="killAll()">KILL SWITCH</button></header><main>
<h2>Fleet</h2><div id="fleet" class="grid"></div>
<h2>Approvals waiting for the owner</h2><div id="appr" class="card mut">none</div>
<h2>Farm queue</h2><div class="card"><table id="queue"></table></div>
<h2>History</h2><div class="card"><table id="hist"></table></div></main>
<script>
const $=id=>document.getElementById(id);
$('tok').value=localStorage.getItem('trm_tok')||'';
function saveTok(){localStorage.setItem('trm_tok',$('tok').value);load()}
function hdr(){const t=localStorage.getItem('trm_tok');return t?{'Authorization':'Bearer '+t}:{}}
async function j(u,opt){const r=await fetch(u,Object.assign({headers:hdr()},opt||{}));if(!r.ok)throw new Error((await r.json()).error||r.status);return r.json()}
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function post(u,b){return j(u,{method:'POST',headers:Object.assign({'Content-Type':'application/json'},hdr()),body:JSON.stringify(b||{})})}
async function answer(id,yes){const q=prompt('Owner\'s own words (recorded in the audit log):');if(q===null)return;
 try{const r=await post('/api/approvals/'+id,{answer:yes?'yes':'no',quote:q});alert(r.status+' '+(r.errors||[]).join('; '))}catch(e){alert(e)};load()}
async function killAll(){const r=prompt('Reason for the global kill switch?');if(r===null)return;try{await post('/api/kill',{reason:r})}catch(e){alert(e)};load()}
async function cam(n,el){try{const r=await fetch('/api/camera/'+encodeURIComponent(n)+'.jpg',{headers:hdr()});if(!r.ok)throw 0;
 el.src=URL.createObjectURL(await r.blob())}catch(e){el.alt='no camera'}}
async function load(){
 try{const h=await j('/api/health');$('health').innerHTML=`<span class="${h.status=='ok'?'ok':'warn'}">${h.status}</span> v${esc(h.version)} up ${h.uptime_s}s`
  +(h.kill_switch&&h.kill_switch.global?' <b class="bad">KILL SWITCH ON</b>':'')}catch(e){$('health').textContent='health: '+e}
 try{const f=await j('/api/fleet');$('fleet').innerHTML=f.printers.map(p=>`<div class="card"><b>${esc(p.name)}</b> <span class="mut">${esc(p.model||'')}</span><br>
  <span class="${p.reachable?(p.has_error?'bad':'ok'):'bad'}">${p.reachable?esc(p.state):'unreachable'}</span>
  ${p.progress_pct!=null&&p.printing?esc(p.progress_pct)+'%':''} ${esc(p.file||'')}<br>
  <span class="mut">nozzle ${esc(p.nozzle&&p.nozzle.actual)}/${esc(p.nozzle&&p.nozzle.target)} bed ${esc(p.bed&&p.bed.actual)}/${esc(p.bed&&p.bed.target)}
  · ${esc(p.material||'?')} ${esc(p.color||'')}</span><br><span class="${p.available?'ok':'mut'}">${p.available?'available':esc((p.unavailable_reasons||[]).join(', '))}</span>
  <img data-p="${esc(p.name)}" alt=""></div>`).join('');document.querySelectorAll('img[data-p]').forEach(i=>cam(i.dataset.p,i))}catch(e){$('fleet').textContent=String(e)}
 try{const a=await j('/api/approvals');$('appr').innerHTML=a.pending.length?a.pending.map(r=>`<div>${esc(r.summary)}<br>
  <button onclick="answer('${esc(r.request_id)}',true)">YES</button> <button class="no" onclick="answer('${esc(r.request_id)}',false)">NO</button></div>`).join('<hr>'):'none'}catch(e){$('appr').textContent=String(e)}
 try{const q=await j('/api/queue');$('queue').innerHTML='<tr><th>job</th><th>status</th><th>printer</th><th>material</th><th>prio</th></tr>'+
  q.farm.filter(x=>!['done','cancelled'].includes(x.status)).map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.status)}</td><td>${esc(x.printer||'')}</td><td>${esc(x.material||'')} ${esc(x.color||'')}</td><td>${esc(x.priority)}</td></tr>`).join('')}catch(e){}
 try{const h=await j('/api/history?limit=15');$('hist').innerHTML='<tr><th>file</th><th>printer</th><th>result</th><th>started</th></tr>'+
  h.history.slice().reverse().map(x=>`<tr><td>${esc(x.file)}</td><td>${esc(x.printer||'default')}</td><td>${esc(x.result)}</td><td>${esc(x.started_at)}</td></tr>`).join('')}catch(e){}
}
load();setInterval(load,10000);
</script></body></html>"""
