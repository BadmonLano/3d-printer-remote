"""Offline test suite: fake slicer, fake printer (HTTP upload + ws 9999), fake camera, fake Telegram.
No network beyond 127.0.0.1, no hardware. Run:  python -m unittest discover -s bots/tests -v
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE.parent / "printer_remote.py"
sys.path.insert(0, str(HERE))
from fake_printer import FakePrinter  # noqa: E402

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None


def write_json(p: Path, d: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


def make_data_dir(root: Path) -> Path:
    dd = root / "Creality Print" / "7.0"
    s = dd / "system" / "Creality"
    write_json(s / "machine" / "fdm_creality_common.json", {"name": "fdm_creality_common", "type": "machine", "gcode_flavor": "klipper"})
    write_json(s / "machine" / "Creality CR-10 SE 0.4 nozzle.json",
               {"name": "Creality CR-10 SE 0.4 nozzle", "type": "machine", "from": "system", "instantiation": "true",
                "inherits": "fdm_creality_common", "printable_area": ["0x0", "220x0", "220x220", "0x220"]})
    write_json(s / "process" / "fdm_process_creality_common.json", {"name": "fdm_process_creality_common", "type": "process", "wall_loops": "2"})
    write_json(s / "process" / "0.20mm Standard @Creality CR-10 SE 0.4 nozzle.json",
               {"name": "0.20mm Standard @Creality CR-10 SE 0.4 nozzle", "type": "process", "from": "system",
                "instantiation": "true", "setting_id": "GP004", "inherits": "fdm_process_creality_common",
                "layer_height": "0.2", "sparse_infill_density": "15%", "brim_width": "0", "brim_type": "no_brim"})
    write_json(s / "filament" / "CR-PETG @Creality CR-10 SE 0.4 nozzle.json",
               {"name": "CR-PETG @Creality CR-10 SE 0.4 nozzle", "type": "filament", "from": "system",
                "instantiation": "true", "nozzle_temperature": ["240"], "hot_plate_temp": ["70"]})
    write_json(dd / "user" / "default" / "process" / "Big gap.json",
               {"name": "Big gap", "from": "User", "inherits": "0.20mm Standard @Creality CR-10 SE 0.4 nozzle",
                "print_settings_id": "Big gap", "enable_support": "1"})
    write_json(dd / "deviceInfo.json", {"groups": [{"list": [{"address": "192.0.2.10", "model": "F003"}]}]})
    return dd


def make_plain_3mf(p: Path, size=(40, 30, 20), offset=(10, 10, 0)):
    sx, sy, sz = size
    ox, oy, oz = offset
    verts = "".join(f'<vertex x="{ox + x*sx}" y="{oy + y*sy}" z="{oz + z*sz}"/>'
                    for x in (0, 1) for y in (0, 1) for z in (0, 1))
    model = (f'<?xml version="1.0"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources><object id="1" type="model"><mesh><vertices>{verts}</vertices><triangles/></mesh></object></resources>'
             f'<build><item objectid="1"/></build></model>')
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", model)
    return p


def make_stl(p: Path, size):
    sx, sy, sz = size
    tris = [((0, 0, 0), (sx, 0, 0), (sx, sy, 0)), ((0, 0, 0), (0, sy, sz), (sx, sy, sz))]
    with open(p, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", 0, 0, 0))
            for v in t:
                f.write(struct.pack("<3f", *v))
            f.write(b"\0\0")
    return p


def make_img(p: Path, objects=0, shade=200):
    im = Image.new("RGB", (320, 240), (shade, shade, shade))
    d = ImageDraw.Draw(im)
    for i in range(objects):
        d.rectangle([40 + i * 60, 60, 90 + i * 60, 180], fill=(20, 20, 20))
    im.save(p, "JPEG")
    return p


class TelegramMock:
    def __init__(self):
        self.calls = []
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                mock.calls.append({"path": self.path, "bytes": len(body), "body": body})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pr_test_"))
        self.dd = make_data_dir(self.tmp)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("PRINTER_", "CAMERA_", "TELEGRAM_", "PR_", "CREALITY_", "AUTO_", "WATCH_",
                                         "FILAMENT_", "PHYSICAL_", "APPROVAL_", "BED", "MAX_Z"))}
        self.env.update({"CREALITY_DATA_DIR": str(self.dd), "CREALITY_PRINT_EXE": str(HERE / "fake_slicer.py"),
                         "PR_STATE_DIR": str(self.tmp / "state"), "PR_OWNER_INBOX": str(self.tmp / "owner_inbox"),
                         "PR_OUTPUT_DIR": str(self.tmp / "gcode"), "APPDATA": str(self.tmp),
                         "PR_FAKE_GUI_RUNNING": "none", "PR_NO_DOTENV": "1"})
        self.tg = TelegramMock()
        self.env.update({"TELEGRAM_BOT_TOKEN": "TEST", "TELEGRAM_CHAT_ID": "1",
                         "TELEGRAM_API_BASE": f"http://127.0.0.1:{self.tg.port}"})

    def tearDown(self):
        self.tg.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, **extra_env):
        e = dict(self.env)
        e.update({k: str(v) for k, v in extra_env.items()})
        r = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, env=e, timeout=120)
        try:
            return json.loads(r.stdout)
        except ValueError:
            self.fail(f"non-JSON output: {r.stdout!r} {r.stderr!r}")

    def packets(self):
        d = self.tmp / "owner_inbox"
        return sorted(d.glob("*.json")) if d.is_dir() else []


class TestCore(Base):
    def test_detect_json_shape(self):
        o = self.run_cli("detect")
        for k in ("status", "job_id", "errors", "exe", "data_dir", "gui_running", "printer_ip"):
            self.assertIn(k, o)
        self.assertEqual(o["printer_ip"], "192.0.2.10")
        self.assertFalse(o["gui_running"])

    def test_presets_list_show_resolved(self):
        o = self.run_cli("presets", "list", "--type", "process")
        names = [p["name"] for p in o["presets"]]
        self.assertIn("Big gap", names)
        self.assertNotIn("fdm_process_creality_common", names)
        o = self.run_cli("presets", "show", "Big gap", "--resolved", "--keys", "layer_height", "walls", "enable_support")
        self.assertEqual(o["settings"], {"layer_height": "0.2", "wall_loops": "2", "enable_support": "1"})

    def test_presets_set_refuses_system_and_backs_up_user(self):
        o = self.run_cli("presets", "set", "0.20mm Standard @Creality CR-10 SE 0.4 nozzle", "--set", "walls=4")
        self.assertEqual(o["status"], "error")
        self.assertIn("clone", o["errors"][0])
        o = self.run_cli("presets", "set", "Big gap", "--set", "walls=4", "--set", "infill=25")
        self.assertEqual(o["status"], "ok", o)
        self.assertTrue(Path(o["backup"]).is_file())
        j = json.loads((self.dd / "user/default/process/Big gap.json").read_text())
        self.assertEqual(j["wall_loops"], "4")
        self.assertEqual(j["sparse_infill_density"], "25%")

    def test_presets_clone_never_overwrites(self):
        o = self.run_cli("presets", "clone", "CR-PETG @Creality CR-10 SE 0.4 nozzle", "My PETG", "--set", "nozzle_temperature=240")
        self.assertEqual(o["status"], "ok", o)
        j = json.loads(Path(o["created"]).read_text())
        self.assertEqual(j["inherits"], "CR-PETG @Creality CR-10 SE 0.4 nozzle")
        self.assertEqual(j["nozzle_temperature"], ["240"])
        o2 = self.run_cli("presets", "clone", "CR-PETG @Creality CR-10 SE 0.4 nozzle", "My PETG")
        self.assertEqual(o2["status"], "error")

    def test_slice_plain_3mf_with_overrides(self):
        m = make_plain_3mf(self.tmp / "plate.3mf")
        o = self.run_cli("slice", str(m), "--walls", "4", "--infill", "20", "--brim", "5", "--supports", "off")
        self.assertEqual(o["status"], "ok", o)
        self.assertEqual(o["overrides"]["wall_loops"], "4")
        self.assertEqual(o["overrides"]["brim_type"], "outer_only")
        st = list(o["stats"].values())[0]
        self.assertEqual(st["filament_g"], 12.34)
        self.assertIn("1h 2m 3s", st["estimated_time"])
        self.assertEqual(o["presets_used"]["process"], "0.20mm Standard @Creality CR-10 SE 0.4 nozzle")
        self.assertTrue(any("--load_settings" == x for x in o["command_preview"]))

    def test_slice_dialect_fallback_to_dash(self):
        m = make_plain_3mf(self.tmp / "plate.3mf")
        o = self.run_cli("slice", str(m), FAKE_SLICER_DIALECT="dash")
        self.assertEqual(o["status"], "ok", o)
        self.assertEqual([a["flag_style"] for a in o["attempts"]], ["underscore", "dash"])

    def test_slice_refuses_when_gui_open(self):
        m = make_plain_3mf(self.tmp / "plate.3mf")
        o = self.run_cli("slice", str(m), PR_FAKE_GUI_RUNNING="4242")
        self.assertEqual(o["status"], "skipped")
        self.assertEqual(o["gui_pids"], [4242])

    def test_slice_crash_reported(self):
        m = make_plain_3mf(self.tmp / "plate.3mf")
        o = self.run_cli("slice", str(m), "--flag-style", "underscore", FAKE_SLICER_CRASH="1")
        self.assertEqual(o["status"], "error")

    def test_split_check(self):
        o = self.run_cli("split-check", str(make_plain_3mf(self.tmp / "s.3mf", size=(100, 50, 20))))
        self.assertTrue(o["fits_as_is"])
        self.assertEqual(o["suggested_pieces"], 1)
        o = self.run_cli("split-check", str(make_stl(self.tmp / "long.stl", (506, 60, 40))))
        self.assertFalse(o["fits_rotated_z"])
        self.assertGreaterEqual(o["suggested_pieces"], 3)


class TestPrinterAndGate(Base):
    def setUp(self):
        super().setUp()
        self.fp = FakePrinter().start()
        self.env.update({"PRINTER_IP": "127.0.0.1", "PRINTER_HTTP_PORT": str(self.fp.http_port),
                         "PRINTER_WS_PORT": str(self.fp.ws_port)})

    def tearDown(self):
        self.fp.stop()
        super().tearDown()

    def test_status_and_upload(self):
        o = self.run_cli("printer", "status")
        self.assertEqual(o["status"], "ok", o)
        self.assertEqual(o["printer"]["model"], "F003")
        g = self.tmp / "egg.gcode"
        g.write_bytes(b"G28\n")
        o = self.run_cli("printer", "upload", str(g))
        self.assertEqual(o["status"], "ok", o)
        self.assertEqual(self.fp.uploads["egg.gcode"], b"G28\n")

    def test_real_cr10se_status_fields(self):
        # values copied from a real CR-10 SE (DWIN V1.1.0.28), idle after a cancelled job
        self.fp.status.update(state=4, deviceState=0, nozzleTemp="23.650000", targetNozzleTemp=0, bedTemp0="22.420000",
                              targetBedTemp0=0, maxNozzleTemp=320, maxBedTemp=130, curFeedratePct=100, curFlowratePct=100,
                              modelVersion="printer hw ver:;printer sw ver:;DWIN hw ver:F003;DWIN sw ver:V1.1.0.28;",
                              lightSw=1, tfCard=1, materialDetect=0, materialStatus=0, usedMaterialLength=0,
                              realTimeSpeed="0.000000", realTimeFlow="0.000000", repoPlrStatus=0, printId="",
                              video=0, video1=0, hostname="printer")
        p = self.run_cli("printer", "status")["printer"]
        self.assertEqual((p["state"], p["idle"], p["printing"]), ("cancelled", True, False))
        self.assertEqual(p["nozzle"], {"actual": 23.65, "target": 0.0, "max": 320.0})
        self.assertEqual(p["firmware_version"], "V1.1.0.28")
        self.assertTrue(p["light_on"])
        self.assertTrue(p["storage_present"])
        self.assertFalse(p["camera_on_printer"])
        self.assertEqual(p["speed_pct"], 100.0)
        o = self.run_cli("printer", "home")
        pf = self.run_cli("preflight", o["request_id"])["preflight"]
        self.assertTrue(pf["passed"], pf)  # state 4 + deviceState 0 counts as idle

    def test_live_stream_and_latest(self):
        self.fp.status.update(state=1, deviceState=1, printProgress=42, nozzleTemp="239.6", targetNozzleTemp=240,
                              bedTemp0="74.8", targetBedTemp0=75, layer=37, TotalLayer=120, printJobTime=1800,
                              printLeftTime=2500, modelFanPct=50, printFileName="egg.gcode",
                              curPosition="X:101.20 Y:88.00 Z:7.40")
        e = dict(self.env)
        r = subprocess.run([sys.executable, str(CLI), "printer", "stream", "--seconds", "2", "--jsonl",
                            "--min-interval", "0.2"], capture_output=True, text=True, env=e, timeout=60)
        lines = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        ups = [l for l in lines if l.get("type") == "update"]
        self.assertGreaterEqual(len(ups), 3, "should receive repeated live pushes")
        self.assertEqual(lines[-1]["type"], "summary")
        u = ups[-1]
        self.assertEqual(u["state"], "printing")
        self.assertEqual(u["progress_pct"], 42)
        self.assertEqual(u["nozzle"], {"actual": 239.6, "target": 240.0, "max": None})
        self.assertEqual(u["bed"]["target"], 75.0)
        self.assertEqual((u["layer"], u["total_layers"]), (37, 120))
        self.assertEqual((u["elapsed_s"], u["remaining_s"]), (1800.0, 2500.0))
        self.assertEqual(u["fans_pct"]["part"], 50.0)
        self.assertEqual(u["position"], {"X": 101.2, "Y": 88.0, "Z": 7.4})
        self.assertFalse(u["has_error"])
        self.fp.status["printProgress"] = 43
        time.sleep(0.5)
        o = self.run_cli("printer", "latest")
        self.assertEqual(o["printer"]["progress_pct"], 42)  # file holds last streamed state
        logs = list((self.tmp / "state" / "logs").glob("printer_stream_*.jsonl"))
        self.assertTrue(logs and logs[0].read_text().count("\n") >= 3)
        self.assertEqual(self.fp.ws_messages[0]["params"], {"ReqPrinterPara": 1}, "stream only reads")

    def approve(self, rid, yes=True, **env):
        return self.run_cli("approve", rid, "--yes" if yes else "--no", "--by", "owner_chat", "--quote", "yes" if yes else "no", **env)

    def test_start_needs_owner_yes_then_preflight(self):
        o = self.run_cli("printer", "start", "egg.gcode")
        self.assertEqual(o["status"], "awaiting_owner", o)
        self.assertIn("Reply YES or NO", o["summary"])
        self.assertEqual(self.fp.ws_messages, [], "nothing may reach the printer before the owner's yes")
        pk = json.loads(self.packets()[0].read_text())
        for k in ("id", "type", "received_at", "lane", "priority", "context"):
            self.assertIn(k, pk)
        self.assertTrue(self.packets()[0].name.startswith("printer_remote - approve start_"))
        self.assertEqual(self.run_cli("pending")["pending"][0]["request_id"], o["request_id"])
        self.assertEqual(self.run_cli("approve", "r_nope", "--yes")["status"], "error")
        ok = self.approve(o["request_id"])
        self.assertEqual(ok["status"], "ok", ok)
        self.assertTrue(ok["preflight"]["passed"])
        names = [c["check"] for c in ok["preflight"]["checks"]]
        for n in ("physical_actions_enabled", "printer_online", "printer_idle", "no_printer_error", "temps_sane", "file_on_printer"):
            self.assertIn(n, names)
        self.assertEqual(ok["answer"]["approved_by"], "owner_chat")
        self.assertTrue(ok["answer"]["answered_at"].endswith("ET"))
        sent = [m for m in self.fp.ws_messages if "opGcodeFile" in m.get("params", {})]
        self.assertEqual(sent[0]["params"]["opGcodeFile"], "printprt:/usr/data/printer_data/gcodes/egg.gcode")
        again = self.approve(o["request_id"])
        self.assertEqual(again["status"], "error")
        self.assertIn("already answered", again["errors"][0])

    def test_owner_no_declines(self):
        o = self.run_cli("printer", "heat", "--nozzle", "200")
        d = self.approve(o["request_id"], yes=False)
        self.assertEqual(d["status"], "declined", d)
        self.assertFalse(any("gcodeCmd" in m.get("params", {}) for m in self.fp.ws_messages))

    def test_preflight_blocks_when_busy_or_error(self):
        self.fp.status.update(state=1, deviceState=1)
        o = self.run_cli("printer", "start", "egg.gcode")
        pf = self.run_cli("preflight", o["request_id"])["preflight"]
        self.assertFalse(pf["passed"])
        self.assertIn("printer_idle", pf["failed"])
        r = self.approve(o["request_id"])
        self.assertEqual(r["status"], "preflight_failed", r)
        self.assertFalse(any("opGcodeFile" in m.get("params", {}) for m in self.fp.ws_messages))
        self.fp.status.update(state=0, deviceState=0, err={"errcode": 2003, "key": 1})
        o = self.run_cli("printer", "home")
        r = self.approve(o["request_id"])
        self.assertIn("no_printer_error", r["preflight"]["failed"])

    def test_preflight_file_and_filament(self):
        g = self.tmp / "other.gcode"
        g.write_bytes(b"G28\n")
        self.run_cli("printer", "upload", str(g), PRINTER_FILES_WAIT_S="1")
        self.run_cli("filament", "add", "--material", "PETG", "--color", "white", "--grams", "500")
        o = self.run_cli("printer", "start", "egg.gcode", "--color", "red", "--material", "PETG")
        r = self.approve(o["request_id"], PRINTER_FILES_WAIT_S="1")
        self.assertEqual(r["status"], "preflight_failed", r)
        self.assertIn("file_on_printer", r["preflight"]["failed"])
        self.assertIn("filament_on_hand", r["preflight"]["failed"])

    def test_preflight_offline(self):
        o = self.run_cli("printer", "pause")
        r = self.approve(o["request_id"], PRINTER_WS_PORT="2", PRINTER_HTTP_PORT="1")  # printer gone
        self.assertEqual(r["status"], "preflight_failed", r)
        self.assertIn("printer_online", r["preflight"]["failed"])

    def test_expired_request(self):
        o = self.run_cli("printer", "cancel", APPROVAL_TTL_MIN="0")
        time.sleep(0.2)
        r = self.approve(o["request_id"])
        self.assertEqual(r["status"], "error")
        self.assertIn("expired", r["errors"][0])
        self.assertFalse(any("stop" in m.get("params", {}) for m in self.fp.ws_messages))

    def test_kill_switch(self):
        o = self.run_cli("printer", "pause", PHYSICAL_ACTIONS_ENABLED="false")
        self.assertEqual(o["status"], "error")
        self.fp.status.update(state=1, deviceState=1)
        o = self.run_cli("printer", "pause")
        r = self.approve(o["request_id"], PHYSICAL_ACTIONS_ENABLED="false")
        self.assertEqual(r["status"], "preflight_failed")
        self.assertIn("physical_actions_enabled", r["preflight"]["failed"])

    def test_heat_bounds(self):
        self.assertEqual(self.run_cli("printer", "heat", "--nozzle", "400")["status"], "error")
        o = self.run_cli("printer", "heat", "--nozzle", "240", "--bed", "75")
        self.assertEqual(o["status"], "awaiting_owner")
        self.approve(o["request_id"])
        self.assertIn({"gcodeCmd": "M104 S240"}, [m.get("params") for m in self.fp.ws_messages])


@unittest.skipIf(Image is None, "Pillow not installed")
class TestCamera(Base):
    def setUp(self):
        super().setUp()
        self.fp = FakePrinter().start()
        self.cam = self.tmp / "cam.jpg"
        make_img(self.cam, objects=0)
        self.env.update({"PRINTER_IP": "127.0.0.1", "PRINTER_HTTP_PORT": str(self.fp.http_port),
                         "PRINTER_WS_PORT": str(self.fp.ws_port), "CAMERA_URL": str(self.cam),
                         "WATCH_WS_WAIT_S": "1"})

    def tearDown(self):
        self.fp.stop()
        super().tearDown()

    def test_snapshot_timestamped(self):
        o = self.run_cli("camera", "snapshot", "--label", "test")
        s = o["snapshot"]
        self.assertTrue(s["timestamp_burned_in"])
        self.assertRegex(Path(s["path"]).name, r"test_\d{8}_\d{6}_ET(_\d+)?\.jpg")
        self.assertTrue(s["taken_at_human"].endswith("ET"))

    def test_bed_check_flags_object_never_approves(self):
        self.assertEqual(self.run_cli("camera", "bed-check")["bed_check"]["verdict"], "no_reference")
        self.run_cli("camera", "set-reference")
        o = self.run_cli("camera", "bed-check")
        self.assertEqual(o["bed_check"]["verdict"], "looks_clear")
        make_img(self.cam, objects=3)
        o = self.run_cli("camera", "bed-check")
        self.assertEqual(o["bed_check"]["verdict"], "possibly_not_clear")
        self.assertFalse(o["bed_check"]["auto_approve"])

    def test_start_request_attaches_snapshot_and_bed_check(self):
        self.run_cli("camera", "set-reference")
        make_img(self.cam, objects=2)
        o = self.run_cli("printer", "start", "egg.gcode")
        self.assertEqual(o["status"], "awaiting_owner")
        self.assertTrue(Path(o["snapshot"]["path"]).is_file())
        self.assertEqual(self.fp.ws_messages, [])
        pf = self.run_cli("preflight", o["request_id"])["preflight"]
        cam = [c for c in pf["checks"] if c["check"] == "bed_clear_camera"][0]
        self.assertIn("possibly_not_clear", cam["detail"])
        self.assertNotEqual(cam["ok"], True, "camera never auto-approves a busy bed")
        make_img(self.cam, objects=0)
        pf = self.run_cli("preflight", o["request_id"])["preflight"]
        cam = [c for c in pf["checks"] if c["check"] == "bed_clear_camera"][0]
        self.assertTrue(cam["ok"], cam)

    def test_watch_big_change_alerts_and_requests_pause(self):
        self.fp.status.update(state=1, deviceState=1, printProgress=10)
        o1 = self.run_cli("watch", "--once", "--reset")
        self.assertEqual(o1["last"]["anomalies"], [])
        make_img(self.cam, objects=4, shade=90)
        o2 = self.run_cli("watch", "--once")
        self.assertEqual(o2["status"], "alert", o2)
        kinds = [a["kind"] for a in o2["last"]["alerted"]]
        self.assertIn("big_frame_change", kinds)
        self.assertTrue(o2["last"]["action"]["pause_requested"])
        self.assertTrue(o2["last"]["action"]["request_id"].startswith("r"))
        self.assertFalse(any(m.get("params", {}).get("pause") == 1 for m in self.fp.ws_messages), "no auto-pause by default")
        self.assertTrue(o2["last"]["telegram"]["sent"])
        self.assertTrue(any("sendPhoto" in c["path"] for c in self.tg.calls))
        names = [p.name for p in self.packets()]
        self.assertTrue(any(n.startswith("printer_remote - ALERT big_frame_change_") for n in names))
        o3 = self.run_cli("watch", "--once")  # cooldown: no duplicate alert
        self.assertEqual(o3["last"]["alerted"], [])

    def test_watch_auto_pause_when_allowed(self):
        self.fp.status.update(state=1, deviceState=1, printProgress=10)
        self.run_cli("watch", "--once", "--reset")
        make_img(self.cam, objects=4, shade=90)
        o = self.run_cli("watch", "--once", AUTO_PAUSE_ON_ANOMALY="true")
        self.assertTrue(o["last"]["action"]["auto_paused"])
        self.assertTrue(any(m.get("params", {}).get("pause") == 1 for m in self.fp.ws_messages))

    def test_watch_stall_and_printer_error(self):
        self.fp.status.update(state=1, deviceState=1, printProgress=33)
        self.run_cli("watch", "--once", "--reset", WATCH_STALL_MINUTES="0")
        o = self.run_cli("watch", "--once", WATCH_STALL_MINUTES="0")
        kinds = [a["kind"] for a in o["last"]["anomalies"]]
        self.assertIn("possible_stall_progress", kinds)
        self.assertIn("possible_stall_camera", kinds)
        self.fp.status["err"] = {"errcode": 2003, "key": 1}
        o = self.run_cli("watch", "--once")
        self.assertIn("printer_error", [a["kind"] for a in o["last"]["anomalies"]])

    def test_watch_temp_runaway_and_heating_without_target(self):
        self.fp.status.update(state=1, deviceState=1, nozzleTemp="262", targetNozzleTemp=240)
        o = self.run_cli("watch", "--once", "--reset")
        self.assertIn("nozzle_temp_runaway", [a["kind"] for a in o["last"]["anomalies"]])
        self.fp.status.update(state=0, deviceState=0, nozzleTemp="25", targetNozzleTemp=0, bedTemp0="65", targetBedTemp0=0)
        self.run_cli("watch", "--once", "--reset")
        self.fp.status.update(bedTemp0="70")
        o = self.run_cli("watch", "--once")
        self.assertIn("bed_heating_without_target", [a["kind"] for a in o["last"]["anomalies"]])
        self.assertNotIn("pause_requested", o["last"].get("action", {}), "idle printer: alert only")

    def test_watch_no_telegram_without_token(self):
        self.fp.status.update(state=1, deviceState=1)
        e = {"TELEGRAM_BOT_TOKEN": ""}
        self.run_cli("watch", "--once", "--reset", **e)
        make_img(self.cam, objects=4, shade=90)
        o = self.run_cli("watch", "--once", **e)
        self.assertFalse(o["last"]["telegram"]["sent"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
