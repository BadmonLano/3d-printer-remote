"""Alerts, operations, plugins, Moonraker stub and the hardware self-test (2.0). Fakes only."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from farm_helpers import FakeML, FakeMoonraker, FarmBase, WebhookSink, core

try:
    from PIL import Image
except ImportError:
    Image = None


class TestAlerts(FarmBase):
    def setUp(self):
        super().setUp()
        self.sink = WebhookSink()

    def tearDown(self):
        self.sink.stop()
        super().tearDown()

    def test_webhook_signed_json(self):
        url = f"http://127.0.0.1:{self.sink.port}/hook"
        o = self.cli("alerts", "test", "--event", "failed", "--text", "spaghetti on se1", ALERT_WEBHOOK_URLS=url,
                     ALERT_WEBHOOK_SECRET="s3cret")
        self.assertTrue(o["alert"]["webhooks"][0]["ok"], o)
        call = self.sink.calls[0]
        body = json.loads(call["body"])
        self.assertEqual(body["event"], "failed")
        sig = "sha256=" + hmac.new(b"s3cret", call["body"], hashlib.sha256).hexdigest()
        hdrs = {k.lower(): v for k, v in call["headers"].items()}
        self.assertEqual(hdrs["x-trm-signature"], sig)
        self.assertTrue(any("alert failed" in p.name for p in self.packets()))

    def test_retry_filter_dedupe_and_formats(self):
        url = f"http://127.0.0.1:{self.sink.port}/hook"
        self.sink.fail_first = 1
        a = core._mod("pr_alerts")
        import os
        os.environ.update(ALERT_WEBHOOK_URLS=url, ALERT_WEBHOOK_FORMAT="slack")
        r = a.emit("done", "egg done", printer="se1", key="j1")
        self.assertTrue(r["webhooks"][0]["ok"])
        self.assertIn("text", json.loads(self.sink.calls[-1]["body"]))
        self.assertEqual(a.emit("done", "egg done", printer="se1", key="j1").get("skipped"), "duplicate within ALERT_DEDUP_MIN")
        os.environ["ALERT_EVENTS"] = "failed"
        self.assertEqual(a.emit("maintenance_due", "x", printer="se1").get("skipped"), "not in ALERT_EVENTS")
        with self.assertRaises(core.Fail):
            a.emit("bogus", "x")

    def test_approval_needed_goes_to_webhook(self):
        url = f"http://127.0.0.1:{self.sink.port}/hook"
        fp = self.fleet([{"name": "se1"}])
        self.cli("printer", "fan", "--pct", "30", "--printer", "se1", ALERT_WEBHOOK_URLS=url)
        self.assertEqual(json.loads(self.sink.calls[-1]["body"])["event"], "approval_needed")


class TestOps(FarmBase):
    def setUp(self):
        super().setUp()
        self.fleet([{"name": "se1", "material": "PETG", "color": "white", "maintenance": {"nozzle_replace": 2}},
                    {"name": "se2", "material": "PLA", "color": "orange"}])

    def _hist(self, printer, hours, result="success", cost=1.5, grams=50):
        h = core.load_store("history", []) if False else json.loads((self.tmp / "state" / "history.json").read_text()) \
            if (self.tmp / "state" / "history.json").exists() else []
        h.append({"id": f"h{len(h)+1:04d}", "printer": printer, "result": result, "duration_s": hours * 3600,
                  "started_epoch": time.time() - hours * 3600 - 10, "est_cost": cost, "est_g": grams})
        (self.tmp / "state").mkdir(exist_ok=True)
        (self.tmp / "state" / "history.json").write_text(json.dumps(h))

    def test_machine_hour_maintenance_per_printer(self):
        self._hist("se1", 3)
        o = self.cli("ops", "maint", "--printer", "se1")
        m = o["maintenance"][0]
        self.assertEqual(m["machine_hours"], 3.0)
        self.assertIn("nozzle_replace", m["due"])
        self.cli("ops", "maint", "--printer", "se1", "--done", "nozzle_replace", "--note", "new 0.4")
        self.assertNotIn("nozzle_replace", self.cli("ops", "maint", "--printer", "se1")["maintenance"][0]["due"])
        o = self.cli("ops", "maint", "--printer", "se2", "--add-hours", "5")
        self.assertEqual(o["maintenance"][0]["machine_hours"], 5.0)

    def test_maintenance_can_block_dispatch(self):
        self._hist("se1", 3)
        o = self.cli("fleet", "status", MAINT_BLOCK_DISPATCH="true")
        se1 = next(r for r in o["printers"] if r["name"] == "se1")
        self.assertFalse(se1["available"])
        self.assertTrue(any("maintenance" in x for x in se1["unavailable_reasons"]))

    def test_spool_location_updates_loaded_material(self):
        self.cli("filament", "add", "--material", "PETG", "--color", "black", "--grams", "900")
        self.cli("filament", "add", "--material", "PETG", "--color", "grey", "--grams", "100")
        o = self.cli("ops", "spool", "--move", "s001", "--to", "printer:se2")
        self.assertEqual(o["printer_loaded"], {"printer": "se2", "material": "PETG", "color": "black"})
        rows = {r["name"]: r for r in self.cli("fleet", "list")["printers"]}
        self.assertEqual(rows["se2"]["color"], "black")
        o = self.cli("ops", "spool", "--move", "s002", "--to", "printer:se2")
        self.assertEqual(o["unloaded"], ["s001"])
        o = self.cli("ops", "spool", "--location", "shelf:unassigned")
        self.assertEqual([s["id"] for s in o["spools"]], ["s001"])
        self.assertIn("s002", o["low"])
        self.assertEqual(self.cli("ops", "spool", "--move", "s001", "--to", "garage")["status"], "error")

    def test_analytics(self):
        self._hist("se1", 2, cost=2.0)
        self._hist("se1", 1, result="failed", cost=1.0)
        self._hist("se2", 4, cost=3.0)
        a = self.cli("ops", "analytics", "--days", "1")["analytics"]
        se1 = next(p for p in a["printers"] if p["printer"] == "se1")
        self.assertEqual(se1["success_rate_pct"], 50.0)
        self.assertEqual(se1["print_hours"], 3.0)
        self.assertEqual(se1["utilization_pct"], 12.5)
        self.assertEqual(se1["cost_per_part_usd"], 2.0)
        self.assertEqual(a["overall"]["cost_per_part_usd"], 2.5)


class TestPlugins(FarmBase):
    def test_slicer_registry_and_stub_argv(self):
        o = self.cli("slicer", "list")
        names = {s["name"]: s for s in o["slicers"]}
        self.assertTrue(names["creality"]["verified"])
        self.assertFalse(names["orca"]["available"])
        pl = core._mod("pr_plugins")
        import os
        from pathlib import Path
        os.environ["ORCASLICER_EXE"] = "/opt/orca"
        argv = pl.get_slicer("orca").build_argv(Path("a.3mf"), Path("out"), ["m_machine.json", "p_process.json", "f_filament.json"])
        self.assertEqual(argv[:4], ["/opt/orca", "--slice", "0", "--outputdir"])
        self.assertIn("m_machine.json;p_process.json", argv)
        o = self.cli("slicer", "slice", "a.3mf", "--backend", "prusa")
        self.assertEqual(o["status"], "error")
        self.assertIn("PRUSASLICER_EXE", o["errors"][0])

    def test_ml_detector_hook(self):
        ml = FakeML(0.9)
        try:
            img = self.tmp / "f.jpg"
            img.write_bytes(b"\xff\xd8x\xff\xd9")
            o = self.cli("detector", "run", "--image", img, "--detector", "ml", DETECTOR_ML_URL=f"http://127.0.0.1:{ml.port}/detect")
            self.assertTrue(o["detection"]["anomaly"], o)
            ml.score = 0.2
            o = self.cli("detector", "run", "--image", img, "--detector", "ml", DETECTOR_ML_URL=f"http://127.0.0.1:{ml.port}/detect")
            self.assertFalse(o["detection"]["anomaly"])
            o = self.cli("detector", "run", "--image", img, "--detector", "ml")
            self.assertEqual(o["detection"]["results"][0]["skipped"], "DETECTOR_ML_URL not set")
        finally:
            ml.stop()

    def test_imagediff_detector(self):
        if Image is None:
            self.skipTest("Pillow not installed")
        a, b = self.tmp / "a.jpg", self.tmp / "b.jpg"
        Image.new("RGB", (64, 64), (0, 0, 0)).save(a)
        Image.new("RGB", (64, 64), (255, 255, 255)).save(b)
        o = self.cli("detector", "run", "--image", b, "--previous", a, "--detector", "imagediff")
        self.assertTrue(o["detection"]["anomaly"], o)

    def test_playbook_pause_request_and_requeue(self):
        self.fleet([{"name": "se1", "material": "PETG", "color": "white"}, {"name": "se2", "material": "PETG", "color": "white"}])
        fp = self.printers["se1"]
        fp.uploads["egg.gcode"] = b"G28"
        self.cli("farm", "add", "egg.gcode", "--material", "PETG", "--color", "white")
        d = self.cli("farm", "dispatch", "--printer", "se1")
        self.cli("approve", d["dispatched"][0]["request_id"], "--yes", "--quote", "y")
        fp.status.update(state=1, deviceState=1)
        o = self.cli("playbook", "run", "--kind", "spaghetti", "--printer", "se1", "--detail", "camera")
        steps = {s["step"]: s for s in o["playbook"]["steps"]}
        self.assertEqual(steps["pause"]["status"], "awaiting_owner")   # owner_yes default: pause is only requested
        self.assertEqual(steps["requeue"]["requeued"][0]["status"], "requeued")
        self.assertFalse([m for m in fp.ws_messages if m.get("params", {}).get("pause") == 1])
        j = self.cli("farm", "list")["jobs"][0]
        self.assertEqual(j["excluded_printers"], ["se1"])
        dry = self.cli("playbook", "run", "--kind", "thermal", "--printer", "se1", "--dry-run")
        self.assertEqual([s["step"] for s in dry["playbook"]["steps"]], ["notify", "cooldown"])

    def test_playbook_thermal_uses_urgent_cooldown(self):
        self.fleet([{"name": "se1"}])
        o = self.cli("playbook", "run", "--kind", "thermal", "--printer", "se1", "--detail", "runaway")
        cd = next(s for s in o["playbook"]["steps"] if s["step"] == "cooldown")
        self.assertTrue(cd["urgent"]["ok"])

    def test_moonraker_backend(self):
        mk = FakeMoonraker()
        try:
            (self.tmp / "printers.json").write_text(json.dumps({"printers": [
                {"name": "rooted", "backend": "moonraker", "moonraker_port": mk.port, "material": "PLA", "color": "red"}]}))
            (self.tmp / "printers.local.json").write_text(json.dumps({"rooted": "127.0.0.1"}))
            o = self.cli("printer", "status", "--printer", "rooted")
            self.assertEqual(o["printer"]["state"], "idle", o)
            g = self.gcode("m.gcode")
            core.set_active_printer("rooted")
            up = core.upload_gcode(core.printer_cfg(), g, "m.gcode")
            core.set_active_printer(None)
            self.assertEqual(up["backend"], "moonraker")
            r = self.cli("printer", "start", "m.gcode", "--printer", "rooted")
            a = self.cli("approve", r["request_id"], "--yes", "--quote", "y")
            self.assertEqual(a["status"], "ok", a)
            self.assertEqual(mk.state, "printing")
            r = self.cli("printer", "pause", "--printer", "rooted")
            self.assertEqual(self.cli("approve", r["request_id"], "--yes", "--quote", "y")["status"], "ok")
            self.assertEqual(mk.state, "paused")
            rows = self.cli("fleet", "status")["printers"]
            self.assertEqual(rows[0]["state"], "paused")
        finally:
            mk.stop()


class TestSelftest(FarmBase):
    def setUp(self):
        super().setUp()
        self.fleet([{"name": "se1"}])
        (self.tmp / "PROTOCOL.md").write_text("# Printer protocol used\n\nintro\n")

    def test_checklist_lists_known_and_unverified(self):
        o = self.cli("selftest", "hardware", "--printer", "se1")
        steps = {c["step"]: c for c in o["checklist"]}
        self.assertTrue(steps["pause"]["verified"])        # proven on the real SE on 2026-11-26
        self.assertFalse(steps["home"]["verified"])
        self.assertIn("cancel", o["needs_someone_at_printer"])
        self.assertIn("upload", o["unattended_ok_next"])

    def test_gated_step_records_after_owner_yes(self):
        r = self.cli("selftest", "hardware", "--run", "fan", "--pct", "60", "--printer", "se1")
        self.assertEqual(r["status"], "awaiting_owner", r)
        a = self.cli("approve", r["request_id"], "--yes", "--quote", "go")
        self.assertEqual(a["selftest"]["result"], "pass", a)
        doc = (self.tmp / "PROTOCOL.md").read_text()
        self.assertIn("SELFTEST:BEGIN", doc)
        self.assertIn("| fan | pass |", doc)
        r = self.cli("selftest", "hardware", "--run", "move", "--printer", "se1")
        a = self.cli("approve", r["request_id"], "--yes", "--quote", "go")
        self.assertEqual(a["selftest"]["result"], "pass", a)

    def test_selftest_never_auto_even_with_auto_policy(self):
        self.policy({"default": "auto_after_preflight", "auto_actions": ["pause"]})
        self.printers["se1"].status.update(state=1, deviceState=1)
        r = self.cli("selftest", "hardware", "--run", "pause", "--printer", "se1")
        self.assertEqual(r["status"], "awaiting_owner")

    def test_free_steps_and_state_guards(self):
        o = self.cli("selftest", "hardware", "--run", "light", "--printer", "se1")
        self.assertEqual(o["selftest"]["result"], "pass", o)
        g = self.gcode("tiny.gcode")
        o = self.cli("selftest", "hardware", "--run", "upload", "--file", g, "--printer", "se1")
        self.assertEqual(o["selftest"]["result"], "pass", o)
        self.assertEqual(self.cli("selftest", "hardware", "--run", "pause", "--printer", "se1")["status"], "error")
        self.assertEqual(self.cli("selftest", "hardware", "--run", "start", "--printer", "se1")["status"], "error")

    def test_manual_record_scrubs_ips(self):
        o = self.cli("selftest", "hardware", "--record", "home", "--result", "pass", "--note", "homed fine at 198.51.100.77")
        self.assertNotIn("198.51.100", json.dumps(o["recorded"]))
        self.assertNotIn("198.51.100", (self.tmp / "PROTOCOL.md").read_text())
        steps = {c["step"]: c for c in o["checklist"]}
        self.assertTrue(steps["home"]["verified"])
