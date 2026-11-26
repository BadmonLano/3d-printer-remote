"""Service mode (2.0): supervised daemon, pid file, health endpoint, dashboard, REST API, tokens and roles."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from farm_helpers import FarmBase, core

svcmod = core._mod("pr_service")
sec = core._mod("pr_security")


class TestService(FarmBase):
    def setUp(self):
        super().setUp()
        self.fleet([{"name": "se1", "material": "PETG", "color": "white"},
                    {"name": "se2", "material": "PLA", "color": "orange"}])
        self.svc = svcmod.Service("127.0.0.1", 0, interval=0.5, watch=False, auto_dispatch=False, duration=None)
        self.res = {}
        self.t = threading.Thread(target=lambda: self.res.update(self.svc.run()), daemon=True)
        self.t.start()
        for _ in range(100):
            if self.svc.httpd and self.svc.fleet_at:
                break
            time.sleep(0.1)
        self.base = f"http://127.0.0.1:{self.svc.port}"

    def tearDown(self):
        self.svc.stop.set()
        self.t.join(10)
        super().tearDown()

    def req(self, method, path, body=None, token=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                ct = resp.headers.get("Content-Type", "")
                raw = resp.read()
                return resp.status, (json.loads(raw) if "json" in ct else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_health_pidfile_and_workers(self):
        code, h = self.req("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(h["status"], "ok", h)
        self.assertTrue(h["workers"]["poller"]["alive"])
        self.assertTrue((self.tmp / "state" / "serve.pid").is_file())
        info = svcmod.running_info()
        self.assertEqual(info["port"], self.svc.port)

    def test_second_instance_refused(self):
        other = svcmod.Service("127.0.0.1", 0, 1, False, False, 1)
        with self.assertRaises(core.Fail):
            other.run()

    def test_dashboard_and_viewer_reads(self):
        code, html = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"3D Printer Remote", html)
        code, f = self.req("GET", "/api/fleet")
        self.assertEqual(code, 200)
        self.assertEqual({p["name"] for p in f["printers"]}, {"se1", "se2"})
        for path in ("/api/queue", "/api/history", "/api/approvals", "/api/analytics", "/api/maintenance", "/api/policy"):
            self.assertEqual(self.req("GET", path)[0], 200, path)
        self.assertEqual(self.req("GET", "/api/audit")[0], 403)  # viewer can't read the audit log

    def test_post_needs_token_and_roles_apply(self):
        self.assertEqual(self.req("POST", "/api/queue", {"file": "x.gcode"})[0], 403)  # local no-token = viewer
        viewer, _ = sec.create_token("v", "viewer")
        op, _ = sec.create_token("op", "operator")
        owner, _ = sec.create_token("boss", "owner")
        self.assertEqual(self.req("POST", "/api/queue", {"file": "x.gcode"}, viewer)[0], 403)
        code, r = self.req("POST", "/api/queue", {"file": "x.gcode", "material": "PETG", "color": "white"}, op)
        self.assertEqual(code, 200, r)
        code, r = self.req("POST", "/api/printers/se1/fan", {"pct": 40}, op)
        self.assertEqual(r["status"], "awaiting_owner", r)
        rid = r["request_id"]
        self.assertEqual(self.req("POST", f"/api/approvals/{rid}", {"answer": "yes", "quote": "ok"}, op)[0], 403)
        code, r = self.req("POST", f"/api/approvals/{rid}", {"answer": "yes", "quote": "yes do it"}, owner)
        self.assertEqual(r["status"], "ok", r)
        self.assertTrue(any(m.get("params", {}).get("gcodeCmd") == "M106 S102" for m in self.printers["se1"].ws_messages))
        code, a = self.req("GET", "/api/audit", token=owner)
        events = [(x["event"], x["actor"]) for x in a["audit"]]
        self.assertIn(("request_created", "token:op"), events)
        self.assertIn(("executed", "token:boss"), events)
        self.assertEqual(self.req("POST", "/api/printers/se1/home", {}, "trm_wrong")[0], 401)

    def test_kill_via_api_and_urgent_cooldown(self):
        op, _ = sec.create_token("op", "operator")
        owner, _ = sec.create_token("boss", "owner")
        self.assertEqual(self.req("POST", "/api/kill", {"reason": "test"}, op)[0], 200)
        code, r = self.req("POST", "/api/printers/se1/pause", {}, op)
        self.assertEqual(code, 400)
        self.assertIn("kill switch", r["error"])
        code, r = self.req("POST", "/api/printers/se2/urgent-cooldown", {"reason": "burning smell"}, op)
        self.assertTrue(r["urgent_cooldown"]["ok"], r)
        self.assertEqual(self.req("POST", "/api/unkill", {}, op)[0], 403)
        self.assertEqual(self.req("POST", "/api/unkill", {}, owner)[0], 200)

    def test_poller_syncs_farm_and_detects_power_loss(self):
        self.printers["se1"].status.update(state=1, deviceState=1, printFileName="egg.gcode")
        time.sleep(1.5)
        self.printers["se1"].power_loss()
        time.sleep(2.5)
        kinds = [e["kind"] for e in self.svc.events]
        self.assertIn("job_lost", kinds)
        self.assertIn("power_loss_resume_pending", kinds)

    def test_supervisor_restarts_crashed_worker(self):
        boom = {"n": 0}

        def bad():
            boom["n"] += 1
            raise SystemExit("crash")  # not caught by the worker loop: the thread dies
        s = svcmod.Service("127.0.0.1", 0, 0.1, False, False, 1.5)
        s.supervise({"bad": (bad, 0.1)})
        self.assertGreaterEqual(s.workers["bad"]["restarts"], 1)
        self.assertGreaterEqual(boom["n"], 2)


class TestServiceGuards(FarmBase):
    def test_non_loopback_needs_tokens(self):
        s = svcmod.Service("0.0.0.0", 0, 1, False, False, 0.5)
        with self.assertRaises(core.Fail) as c:
            s.run()
        self.assertIn("token", str(c.exception))

    def test_serve_cli_duration_and_status(self):
        o = self.cli("serve", "--port", "0", "--duration", "1.2", "--interval", "0.5")
        self.assertEqual(o["status"], "ok", o)
        self.assertFalse((self.tmp / "state" / "serve.pid").exists())
        self.assertFalse(self.cli("serve", "--status")["running"])

    def test_tokens_hashed_and_revocable(self):
        o = self.cli("token", "create", "--name", "phone", "--role", "viewer")
        tok = o["token"]
        raw = (self.tmp / "state" / "tokens.json").read_text()
        self.assertNotIn(tok, raw)
        self.assertTrue(sec.verify(tok))
        self.cli("token", "revoke", "phone")
        self.assertIsNone(sec.verify(tok))
        self.assertEqual(self.cli("token", "list", PR_ACTOR_ROLE="viewer")["status"], "error")
