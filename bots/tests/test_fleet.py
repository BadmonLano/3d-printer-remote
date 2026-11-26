"""Fleet (2.0): printers.json, --printer targeting, fleet status/stream, read-only discovery. Multi-instance fakes."""
from __future__ import annotations

import json

from farm_helpers import FarmBase, core


class TestFleet(FarmBase):
    def setUp(self):
        super().setUp()
        self.fleet([{"name": "se1", "material": "PETG", "color": "white", "groups": ["garage"]},
                    {"name": "se2", "material": "PLA", "color": "orange", "nozzle_mm": 0.6}])

    def test_list_never_shows_ips(self):
        o = self.cli("fleet", "list")
        self.assertEqual(o["count"], 2)
        self.assertNotIn("127.0.0.1", json.dumps(o["printers"]))
        self.assertTrue(all(p["ip_configured"] for p in o["printers"]))

    def test_printer_flag_targets_the_right_unit(self):
        self.printers["se2"].status.update(hostname="unit-two")
        a = self.cli("printer", "status", "--printer", "se2")
        self.assertEqual(a["status"], "ok", a)
        self.assertEqual(a["printer"]["hostname"], "unit-two")
        b = self.cli("--printer", "se1", "printer", "status")
        self.assertEqual(b["printer"]["hostname"], "fake-cr10se")
        self.assertTrue((self.tmp / "state" / "printers" / "se2" / "printer_latest.json").is_file())

    def test_unknown_printer_is_a_clean_error(self):
        o = self.cli("printer", "status", "--printer", "nope")
        self.assertEqual(o["status"], "error")
        self.assertIn("unknown printer", o["errors"][0])

    def test_fleet_status_parallel_with_availability(self):
        self.printers["se2"].status.update(state=1, deviceState=1, printFileName="x.gcode")
        o = self.cli("fleet", "status")
        self.assertEqual(o["status"], "ok", o)
        rows = {r["name"]: r for r in o["printers"]}
        self.assertTrue(rows["se1"]["available"])
        self.assertFalse(rows["se2"]["available"])
        self.assertIn("busy", rows["se2"]["unavailable_reasons"][0])
        self.assertEqual(o["summary"]["available"], ["se1"])

    def test_fleet_status_marks_unreachable_and_killed(self):
        self.printers["se2"].stop()
        del self.printers["se2"]
        self.cli("policy", "kill", "--printer", "se1", "--reason", "nozzle swap")
        o = self.cli("fleet", "status", PR_WS_CONNECT_RETRIES="0", PR_STATUS_RETRIES="0")
        rows = {r["name"]: r for r in o["printers"]}
        self.assertFalse(rows["se2"]["reachable"])
        self.assertIn("kill switch", rows["se1"]["unavailable_reasons"])

    def test_gated_request_runs_on_its_printer_only(self):
        r = self.cli("printer", "fan", "--pct", "50", "--printer", "se2")
        self.assertEqual(r["params"]["printer"], "se2")
        o = self.cli("approve", r["request_id"], "--yes", "--quote", "yes")  # no --printer: comes from the request
        self.assertEqual(o["status"], "ok", o)
        m1 = [m for m in self.printers["se1"].ws_messages if "gcodeCmd" in m.get("params", {})]
        m2 = [m for m in self.printers["se2"].ws_messages if "gcodeCmd" in m.get("params", {})]
        self.assertEqual(m1, [])
        self.assertEqual(m2[0]["params"]["gcodeCmd"], "M128".replace("M128", "M106 S128"))

    def test_fleet_stream_jsonl_tags_printer(self):
        o = self.cli("fleet", "stream", "--seconds", "1.2", "--min-interval", "0.2")
        self.assertEqual(set(o["printers"]), {"se1", "se2"})
        self.assertTrue(all(v.get("updates", 0) > 0 for v in o["printers"].values()), o)

    def test_discover_finds_fake_and_saves_ip_locally_only(self):
        port = self.printers["se1"].ws_port
        (self.tmp / "printers.local.json").write_text("{}")
        (self.tmp / "printers.json").write_text(json.dumps({"printers": []}))
        o = self.cli("fleet", "discover", "--subnet", "127.0.0.1/32", "--port", port, "--save")
        self.assertEqual(o["status"], "ok", o)
        self.assertEqual(len(o["found"]), 1)
        self.assertTrue(o["found"][0]["creality_os"])
        self.assertEqual(o["saved"], ["fake-cr10se"])
        self.assertNotIn("127.0.0.1", (self.tmp / "printers.json").read_text())
        self.assertIn("127.0.0.1", (self.tmp / "printers.local.json").read_text())

    def test_discover_refuses_huge_subnets(self):
        o = self.cli("fleet", "discover", "--subnet", "10.0.0.0/16")
        self.assertEqual(o["status"], "error")

    def test_fleet_add_set_keeps_ip_out_of_config(self):
        o = self.cli("fleet", "add", "se3", "--material", "petg", "--color", "Black", "--ip", "127.0.0.1", "--bed", "300x300x400")
        self.assertEqual(o["printer"]["material"], "PETG")
        self.assertEqual(o["printer"]["color"], "black")
        self.assertNotIn("127.0.0.1", (self.tmp / "printers.json").read_text())
        o = self.cli("fleet", "set", "se3", "--color", "grey")
        self.assertEqual(o["printer"]["color"], "grey")

    def test_no_fleet_file_is_classic_single_printer(self):
        (self.tmp / "printers.json").unlink()
        import os
        fp = self.printers["se1"]
        os.environ.update({"PRINTER_IP": "127.0.0.1", "PRINTER_WS_PORT": str(fp.ws_port)})
        o = self.cli("fleet", "status")
        self.assertEqual([r["name"] for r in o["printers"]], ["default"])
        self.assertTrue(o["printers"][0]["reachable"])
        self.assertTrue((self.tmp / "state" / "printer_latest.json").is_file())

    def test_per_printer_camera(self):
        from pathlib import Path
        img = self.tmp / "cam.jpg"
        img.write_bytes(b"\xff\xd8fakejpeg\xff\xd9")
        rows = json.loads((self.tmp / "printers.json").read_text())["printers"]
        rows[1]["cameras"] = [{"name": "top", "url_env": "CAM_SE2_TOP"}]
        (self.tmp / "printers.json").write_text(json.dumps({"printers": rows}))
        o = self.cli("camera", "snapshot", "--printer", "se2", "--camera", "top", CAM_SE2_TOP=str(img))
        self.assertEqual(o["status"], "ok", o)
        self.assertIn(str(Path("printers") / "se2"), o["snapshot"]["raw_path"])
