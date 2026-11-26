"""v1.1 features: gcode inspector, queue + filament swaps, history, inventory, plates by color, recommender,
maintenance, library, timelapse, printer queries, gcode console gate, job events (first layer / ETA / done), health.
Offline only (fake printer, fake camera, fake Telegram)."""
from __future__ import annotations

import json
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_printer import FakePrinter  # noqa: E402
from test_printer_remote import Base, make_img, make_plain_3mf, Image  # noqa: E402

GCODE = """; HEADER_BLOCK_START
; model printing time: 0h 30m 0s; total estimated time: 0h 32m 0s
; total layer number: 50
; HEADER_BLOCK_END
; filament_type = PETG
M140 S75
M104 S250
M190 S75
M109 S250
G90
;LAYER_CHANGE
G1 X10 Y10 Z0.2 F3000
G1 X100 Y10 E5
;LAYER_CHANGE
G1 X100 Y100 Z0.4 E10
M600
;LAYER_CHANGE
G1 X10 Y100 Z0.6 E15
; filament used [mm] = 5000.0
; filament used [g] = 15.0
"""


def gfile(d: Path, name="job.gcode", text=GCODE) -> Path:
    p = d / name
    p.write_text(text)
    return p


class TestOffline(Base):
    def test_gcode_inspect_flags_250_petg_and_cost(self):
        o = self.run_cli("gcode", "inspect", str(gfile(self.tmp)), FILAMENT_COST_PER_KG="25")
        ins = o["inspect"]
        self.assertEqual(ins["material"], "PETG")
        self.assertEqual(ins["max_nozzle"], 250)
        self.assertEqual(ins["layers"], 3)
        self.assertEqual(ins["estimated_s"], 1800)
        self.assertEqual(ins["cost_usd"], 0.38)
        self.assertTrue(any("above the usual PETG max 245" in w for w in ins["warnings"]))
        self.assertTrue(any("filament-change" in w for w in ins["warnings"]))
        self.assertEqual(ins["pauses"][0]["cmd"], "M600")

    def test_inventory_and_check(self):
        self.assertIsNone(self.run_cli("filament", "check", "--color", "red")["check"]["ok"])
        self.run_cli("filament", "add", "--material", "petg", "--color", "White", "--grams", "800", "--cost", "22")
        o = self.run_cli("filament", "check", "--material", "PETG", "--color", "red", "--grams", "20")
        self.assertFalse(o["check"]["ok"])
        self.assertTrue(o["warnings"])
        o = self.run_cli("filament", "use", "s001", "--grams", "700")
        self.assertEqual(o["spool"]["grams_left"], 100)
        self.assertEqual(len(self.run_cli("filament", "list")["low"]), 1)
        self.run_cli("filament", "archive", "s001")
        self.assertEqual(self.run_cli("filament", "list", "--all")["spools"][0]["archived"], True)

    def test_plates_by_color(self):
        src = self.tmp / "egg.3mf"
        objs = ""
        for i, name in enumerate(["egg_top", "egg_bottom", "fried_egg_yolk"], 1):
            objs += (f'<object id="{i}" name="{name}" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/>'
                     f'<vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="5"/></vertices>'
                     f'<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>')
        items = "".join(f'<item objectid="{i}"/>' for i in (1, 2, 3))
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("3D/3dmodel.model", f'<?xml version="1.0"?><model unit="millimeter"><resources>{objs}</resources><build>{items}</build></model>')
        o = self.run_cli("plates", "by-color", str(src), "--map", "yolk=red", "--default", "white", "--order", "white,red")
        self.assertEqual([p["color"] for p in o["plates"]], ["white", "red"])
        self.assertEqual(o["plates"][1]["objects"], ["fried_egg_yolk"])
        with zipfile.ZipFile(o["plates"][1]["file"]) as z:
            m = z.read("3D/3dmodel.model").decode()
        self.assertIn("fried_egg_yolk", m)
        self.assertNotIn("egg_top", m)
        self.assertIn('objectid="3"', m)
        self.assertNotIn('objectid="1"', m)

    def test_recommend(self):
        o = self.run_cli("recommend", "--part", "car-exterior", "--material", "petg")
        self.assertEqual(o["process_overrides"]["wall_loops"], "5")
        self.assertTrue(any("ASA" in n for n in o["notes"]))
        self.assertEqual(self.run_cli("recommend", "--part", "nope")["status"], "error")

    def test_maintenance(self):
        self.run_cli("maint", "add-hours", "55")
        o = self.run_cli("maint", "status")
        self.assertIn("cold_pull", o["maintenance"]["due"])
        o = self.run_cli("maint", "done", "cold_pull", "--note", "clean pull")
        self.assertNotIn("cold_pull", o["maintenance"]["due"])
        for _ in range(3):
            o = self.run_cli("maint", "jam", "--note", "stuck in nozzle")
        self.assertTrue(o["checklist"])
        self.assertTrue(any("jams" in w for w in o["warnings"]))

    def test_library(self):
        lib = self.tmp / "lib"
        lib.mkdir()
        make_plain_3mf(lib / "small_part.3mf", size=(50, 50, 20))
        make_plain_3mf(lib / "big_canard.3mf", size=(500, 80, 30))
        o = self.run_cli("library", "scan", "--root", str(lib))
        self.assertEqual(o["count"], 2)
        self.assertEqual(o["not_fitting"], ["big_canard.3mf"])
        o = self.run_cli("library", "find", "canard")
        self.assertEqual(o["files"][0]["pieces"], 3)
        self.assertEqual(self.run_cli("library", "find", "--fits")["count"], 1)

    @unittest.skipIf(Image is None, "Pillow not installed")
    def test_timelapse_gif(self):
        snaps = self.tmp / "state" / "snapshots"
        snaps.mkdir(parents=True)
        for i in range(4):
            make_img(snaps / f"watch_2026112{i}.jpg", objects=i)
        o = self.run_cli("timelapse", "build", "--gif", "--fps", "5")
        self.assertEqual(o["status"], "ok", o)
        self.assertEqual(o["frames"], 4)
        self.assertTrue(Path(o["video"]).is_file())

    def test_health_offline(self):
        o = self.run_cli("health", "--packet")
        self.assertEqual(o["status"], "ok", o)
        for k in ("slicer", "printer", "queue", "filament", "maintenance", "history", "flags"):
            self.assertIn(k, o["health"])
        self.assertTrue(Path(o["packet"]).is_file())


class PrinterBase(Base):
    def setUp(self):
        super().setUp()
        self.fp = FakePrinter().start()
        self.cam = self.tmp / "cam.jpg"
        if Image:
            make_img(self.cam)
        self.env.update({"PRINTER_IP": "127.0.0.1", "PRINTER_HTTP_PORT": str(self.fp.http_port),
                         "PRINTER_WS_PORT": str(self.fp.ws_port), "PRINTER_FILES_WAIT_S": "1.5",
                         "WATCH_WS_WAIT_S": "1"})
        if Image:
            self.env["CAMERA_URL"] = str(self.cam)

    def tearDown(self):
        self.fp.stop()
        super().tearDown()


class TestWithPrinter(PrinterBase):
    def test_queue_swap_flow_history_and_done_notice(self):
        white = gfile(self.tmp, "white.gcode", GCODE.replace("M600\n", ""))
        red = gfile(self.tmp, "red.gcode", GCODE.replace("M600\n", ""))
        self.run_cli("filament", "add", "--material", "PETG", "--color", "white", "--grams", "500")
        o = self.run_cli("queue", "add", str(white), "--color", "white", "--note", "egg shells")
        self.assertEqual(o["status"], "ok", o)
        o = self.run_cli("queue", "add", str(red), "--color", "red", "--note", "yolk")
        self.assertTrue(any("no PETG red spool" in w for w in o["warnings"]))
        o = self.run_cli("queue", "next")
        self.assertEqual(o["status"], "awaiting_owner", o)
        self.assertIn("load white PETG", o["summary"])
        self.assertTrue(any(n.startswith("white_") for n in self.fp.uploads))
        self.assertFalse(any("opGcodeFile" in m.get("params", {}) for m in self.fp.ws_messages))
        self.assertEqual(self.run_cli("queue", "next")["status"], "error", "one job at a time")
        c = self.run_cli("approve", o["request_id"], "--yes")
        self.assertEqual(c["status"], "ok", c)
        self.assertEqual(self.run_cli("history", "stats")["stats"]["open_job"]["color"], "white")
        # watch sees printing then complete
        self.fp.status.update(state=1, deviceState=1, printProgress=50, layer=3, TotalLayer=50, printLeftTime=300)
        w = self.run_cli("watch", "--once", "--reset")
        ev = json.dumps(w["last"]["events"])
        self.assertIn("first_layer", ev)
        self.assertIn("eta", ev)
        self.fp.status.update(state=2, deviceState=0, printProgress=100)
        w = self.run_cli("watch", "--once")
        fin = [e for e in w["last"]["events"] if isinstance(e, dict) and "finished" in e]
        self.assertTrue(fin)
        names = [p.name for p in self.packets()]
        self.assertTrue(any(n.startswith("printer_remote - print success_") for n in names))
        pk = json.loads(sorted(p for p in self.packets() if "print success" in p.name)[0].read_text())
        self.assertIn("Clear the bed", pk["context"]["summary"])
        self.assertIn("Next in queue: red.gcode in red", pk["context"]["summary"])
        st = self.run_cli("history", "stats")["stats"]
        self.assertEqual((st["success"], st["finished"]), (1, 1))
        self.assertGreater(self.run_cli("maint", "status")["maintenance"]["prints"], 0)
        inv = self.run_cli("filament", "list")["spools"][0]
        self.assertEqual(inv["grams_left"], 485.0)
        o = self.run_cli("queue", "next")
        self.assertIn("load red PETG (was white)", o["summary"])

    def test_printer_files_history_info(self):
        g = gfile(self.tmp)
        up = self.run_cli("printer", "upload", str(g))
        self.assertTrue(up["verified_on_printer"], up)
        o = self.run_cli("printer", "files")
        self.assertEqual(o["files"][0]["name"], "job.gcode")
        o = self.run_cli("printer", "history")
        self.assertIn("historyList", o["reply"])
        o = self.run_cli("printer", "info")
        self.assertEqual(o["info"]["model"], "F003")
        self.assertEqual(o["info"]["creality_print_saved"][0]["address"], "192.0.2.10")

    def test_gcode_console_gate(self):
        o = self.run_cli("printer", "gcode", "M105")
        self.assertEqual(o["status"], "ok")
        self.assertTrue(o["readonly"])
        o = self.run_cli("printer", "gcode", "G28")
        self.assertEqual(o["status"], "awaiting_owner")
        self.assertNotIn({"gcodeCmd": "G28"}, [m.get("params") for m in self.fp.ws_messages])
        self.run_cli("approve", o["request_id"], "--yes")
        self.assertIn({"gcodeCmd": "G28"}, [m.get("params") for m in self.fp.ws_messages])
        h = self.run_cli("printer", "home")
        self.assertEqual(h["status"], "awaiting_owner")
        f = self.run_cli("printer", "fan", "--pct", "50")
        self.run_cli("approve", f["request_id"], "--yes")
        self.assertIn({"gcodeCmd": "M106 S128"}, [m.get("params") for m in self.fp.ws_messages])

    def test_paused_notice_and_health_probe(self):
        self.fp.status.update(state=1, deviceState=1, layer=1, printProgress=1)
        self.run_cli("watch", "--once", "--reset")
        self.fp.status.update(state=5)
        w = self.run_cli("watch", "--once")
        self.assertIn("paused", json.dumps(w["last"]["events"]))
        h = self.run_cli("health", "--probe")
        self.assertEqual(h["health"]["printer"]["state"], "paused")
        self.assertFalse(h["health"]["printer"]["stale"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestGateSecurity(PrinterBase):
    """Kill switch covers auto-pause; gated move; probe reports unreachable; no confirm-code machinery left."""

    def test_no_code_in_request(self):
        o = self.run_cli("printer", "heat", "--nozzle", "200")
        self.assertEqual(o["status"], "awaiting_owner", o)
        self.assertNotIn("code", o)
        req = json.loads(next((self.tmp / "state" / "pending").glob("*.json")).read_text())
        self.assertNotIn("code_hash", req)
        self.assertIsNone(req["answer"])
        self.assertEqual(self.tg.calls, [], "no Telegram unless APPROVAL_TELEGRAM=true")

    def test_kill_switch_blocks_auto_pause(self):
        self.env.update({"AUTO_PAUSE_ON_ANOMALY": "true", "PHYSICAL_ACTIONS_ENABLED": "false"})
        self.fp.status.update(state=1, deviceState=1, nozzleTemp="262", targetNozzleTemp=240, printProgress=10)
        w = self.run_cli("watch", "--once", "--reset")
        self.assertFalse(w["last"]["action"].get("auto_paused"), w["last"])
        self.assertIn("switched off", w["last"]["action"].get("error", ""))
        self.assertNotIn({"pause": 1}, [m.get("params") for m in self.fp.ws_messages])

    def test_gated_move_bounds_and_send(self):
        self.assertEqual(self.run_cli("printer", "move", "--x", "300")["status"], "error")
        self.assertEqual(self.run_cli("printer", "move", "--relative", "--z", "60")["status"], "error")
        self.assertEqual(self.run_cli("printer", "move")["status"], "error")
        o = self.run_cli("printer", "move", "--relative", "--z", "10", "--feed", "600")
        self.assertEqual(o["status"], "awaiting_owner", o)
        self.assertNotIn("G1 Z10 F600", [m.get("params", {}).get("gcodeCmd") for m in self.fp.ws_messages])
        r = self.run_cli("approve", o["request_id"], "--yes")
        self.assertEqual(r["status"], "ok", r)
        sent = [m.get("params", {}).get("gcodeCmd") for m in self.fp.ws_messages]
        i = sent.index("G91")
        self.assertEqual(sent[i:i + 3], ["G91", "G1 Z10 F600", "G90"])

    def test_probe_unreachable_is_error(self):
        self.env.update({"PRINTER_IP": "127.0.0.1", "PRINTER_HTTP_PORT": "1", "PRINTER_WS_PORT": "2",
                         "PR_PROBE_PORTS": "1,2", "PR_PROBE_TIMEOUT_S": "0.3"})
        o = self.run_cli("printer", "probe")
        self.assertEqual(o["status"], "error", o)
        self.assertIn("not reachable", " ".join(o["errors"]))


class TestFilamentMoves(PrinterBase):
    """1.2.0: gated extrude / retract / load / unload with 300 C preheat, and cooldown."""

    def gsent(self):
        return [m.get("params", {}).get("gcodeCmd") for m in self.fp.ws_messages if "gcodeCmd" in m.get("params", {})]

    def setUp(self):
        super().setUp()
        self.env.update({"FILAMENT_SOAK_S": "0", "FILAMENT_HEAT_TIMEOUT_S": "20"})

    def test_bounds(self):
        self.assertEqual(self.run_cli("filament", "extrude")["status"], "error")
        self.assertEqual(self.run_cli("filament", "extrude", "--mm", "150")["status"], "error")
        self.assertEqual(self.run_cli("filament", "retract", "--mm", "10", "--temp", "330")["status"], "error")
        self.assertEqual(self.run_cli("filament", "retract", "--mm", "10", "--temp", "301")["status"], "error")
        self.assertEqual(self.run_cli("filament", "retract", "--mm", "10", "--speed", "50")["status"], "error")
        o = self.run_cli("filament", "retract", "--mm", "10")
        self.assertEqual(o["status"], "awaiting_owner", o)
        self.assertEqual(o["params"]["temp"], 240.0, "no material given: PETG 240")
        self.assertIn("heat nozzle to 240 C (PETG)", o["summary"])
        self.assertIn("nozzle off", o["summary"])
        self.assertEqual(self.gsent(), [], "nothing sent before the owner's yes")

    def test_retract_preheats_moves_and_cools(self):
        o = self.run_cli("filament", "retract", "--mm", "10", "--speed", "10")
        r = self.run_cli("approve", o["request_id"], "--yes", "--by", "owner_chat", "--quote", "test the retract")
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(self.gsent(), ["M104 S240", "M83", "G1 E-10 F600", "M82", "M104 S0"])
        res = r["result"]
        self.assertTrue(res["completed"])
        self.assertEqual(res["nozzle_target_set_to"], 0)
        self.assertGreaterEqual(max(t["nozzle"] for t in res["timeline"]), 237)
        self.assertIn("reached_temp_after_s", res)

    def test_unload_chunks_and_hold(self):
        o = self.run_cli("filament", "unload", "--speed", "10", "--hold", "--purge")
        self.assertEqual(o["params"]["chunks"], [5.0, -45.0, -35.0])
        self.run_cli("approve", o["request_id"], "--yes")
        g = self.gsent()
        self.assertEqual(g[:5], ["M104 S300", "M83", "G1 E5 F600", "G1 E-45 F600", "G1 E-35 F600"])
        self.assertNotIn("M104 S0", g, "--hold keeps the nozzle hot")

    def test_heat_timeout_turns_nozzle_off(self):
        self.fp.heat_rate = 0
        o = self.run_cli("filament", "extrude", "--mm", "5")
        r = self.run_cli("approve", o["request_id"], "--yes", FILAMENT_HEAT_TIMEOUT_S="2")
        self.assertEqual(r["status"], "error", r)
        g = self.gsent()
        self.assertEqual(g, ["M104 S240", "M104 S0", "M140 S0"], "failure path uses the urgent cooldown")
        self.assertTrue(any("URGENT cooldown" in p.name for p in self.packets()))

    def test_refused_while_printing_and_cooldown(self):
        self.fp.status.update(state=1, deviceState=1)
        o = self.run_cli("filament", "load")
        r = self.run_cli("approve", o["request_id"], "--yes")
        self.assertEqual(r["status"], "preflight_failed")
        self.assertIn("printer_idle_or_paused", r["preflight"]["failed"])
        c = self.run_cli("printer", "cooldown")
        self.assertEqual(c["status"], "awaiting_owner")
        self.assertEqual(self.run_cli("approve", c["request_id"], "--yes")["status"], "ok")
        self.assertEqual(self.gsent(), ["M104 S0", "M140 S0"])


class TestPreheat(Base):
    """1.2.1: per-material preheat, explicit 300 C purge, shared 300 C nozzle ceiling (no printer needed)."""

    def temp(self, *extra, **env):
        o = self.run_cli("filament", "retract", "--mm", "5", *extra, **env)
        return o["params"]["temp"] if o["status"] == "awaiting_owner" else o

    def test_material_defaults(self):
        for mat, t in (("PLA", 210), ("pla", 210), ("PETG", 240), ("ABS", 245), ("ASA", 250), ("TPU", 225)):
            self.assertEqual(self.temp("--material", mat), t, mat)
        self.assertEqual(self.temp(), 240)
        self.assertEqual(self.temp(FILAMENT_MATERIAL="PLA"), 210)
        self.assertEqual(self.temp(FILAMENT_PREHEAT_C="300"), 300, "owner's personal override via env")
        self.assertEqual(self.temp("--material", "PLA", FILAMENT_PREHEAT_C="300"), 210, "--material wins")
        bad = self.temp("--material", "NYLON")
        self.assertEqual(bad["status"], "error")
        self.assertIn("unknown material", bad["errors"][0])

    def test_purge_and_ceiling(self):
        self.assertEqual(self.temp("--purge"), 300)
        self.assertEqual(self.temp("--temp", "300"), 300)
        self.assertEqual(self.temp("--material", "PLA", "--purge"), 300, "--purge wins over the material")
        for t in ("301", "320"):
            self.assertEqual(self.temp("--temp", t)["status"], "error", t)
        self.assertEqual(self.temp("--purge", FILAMENT_MAX_C="280")["status"], "error", "lower FILAMENT_MAX_C applies")
        self.assertEqual(self.temp("--temp", "290", FILAMENT_MAX_C="350"), 290)
        self.assertEqual(self.temp("--temp", "310", FILAMENT_MAX_C="350")["status"], "error", "cannot raise past 300")

    def test_heat_shares_the_ceiling(self):
        self.assertEqual(self.run_cli("printer", "heat", "--nozzle", "301")["status"], "error")
        o = self.run_cli("printer", "heat", "--nozzle", "290")
        self.assertEqual(o["status"], "awaiting_owner", o)
        self.assertIn("purge territory", o["summary"])
        self.assertTrue(o["warnings"])
        o = self.run_cli("printer", "heat", "--nozzle", "240")
        self.assertEqual(o["warnings"], [])


class TestUrgentCooldown(PrinterBase):
    """1.2.1: `printer cooldown --urgent --reason` skips the owner yes; nothing else can be urgent."""

    def gsent(self):
        return [m.get("params", {}).get("gcodeCmd") for m in self.fp.ws_messages if "gcodeCmd" in m.get("params", {})]

    def test_urgent_runs_now_and_logs(self):
        o = self.run_cli("printer", "cooldown", "--urgent", "--reason", "burning smell", "--fan")
        self.assertEqual(o["status"], "ok", o)
        self.assertEqual(self.gsent(), ["M104 S0", "M140 S0", "M106 S255"])
        self.assertEqual(self.run_cli("pending")["pending"], [], "no approval request")
        pk = [p for p in self.packets() if "URGENT cooldown" in p.name]
        self.assertEqual(len(pk), 1)
        ctx = json.loads(pk[0].read_text())["context"]
        self.assertIn("burning smell", ctx["summary"])
        self.assertTrue(ctx["output"]["at"].endswith("ET"))
        log = json.loads((self.tmp / "state" / "safety_log.json").read_text())
        self.assertEqual(log[-1]["reason"], "burning smell")

    def test_urgent_needs_reason_and_works_with_kill_switch(self):
        self.assertEqual(self.run_cli("printer", "cooldown", "--urgent")["status"], "error")
        self.assertEqual(self.gsent(), [])
        o = self.run_cli("printer", "cooldown", "--urgent", "--reason", "jam", PHYSICAL_ACTIONS_ENABLED="false")
        self.assertEqual(o["status"], "ok", "removing heat is allowed even with the kill switch off")
        self.assertEqual(self.gsent(), ["M104 S0", "M140 S0"])

    def test_non_urgent_cooldown_still_gated(self):
        o = self.run_cli("printer", "cooldown")
        self.assertEqual(o["status"], "awaiting_owner")
        self.assertEqual(self.gsent(), [])

    def test_urgent_rejected_elsewhere(self):
        for args in (["printer", "pause", "--urgent"], ["printer", "home", "--urgent"],
                     ["printer", "heat", "--nozzle", "200", "--urgent"], ["filament", "retract", "--mm", "5", "--urgent"]):
            o = self.run_cli(*args)
            self.assertEqual(o["status"], "error", args)
            self.assertIn("only applies to `printer cooldown`", " ".join(o["errors"]))
        self.assertEqual(self.gsent(), [])
        self.assertEqual(self.run_cli("pending")["pending"], [])

    def test_watch_auto_cooldown_on_runaway(self):
        self.fp.status.update(state=0, deviceState=0, nozzleTemp="25", targetNozzleTemp=0, bedTemp0="65", targetBedTemp0=0)
        self.run_cli("watch", "--once", "--reset")
        self.fp.status.update(bedTemp0="70")
        w = self.run_cli("watch", "--once")
        self.assertNotIn("urgent_cooldown", w["last"].get("action", {}), "off by default")
        self.assertEqual(self.gsent(), [])
        self.run_cli("watch", "--once", "--reset")
        self.fp.status.update(bedTemp0="76")
        w = self.run_cli("watch", "--once", AUTO_COOLDOWN_ON_ANOMALY="true")
        self.assertTrue(w["last"]["action"]["urgent_cooldown"]["ok"], w["last"])
        self.assertEqual(self.gsent(), ["M104 S0", "M140 S0"])
