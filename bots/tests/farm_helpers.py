"""Shared helpers for the 2.0 farm tests: in-process CLI runner, temp state, fake Moonraker, webhook sink, fake ML."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import printer_remote as core  # noqa: E402
from fake_printer import FakePrinter  # noqa: E402

CLEAR = ("PRINTER_", "CAMERA_", "TELEGRAM_", "PR_", "CREALITY_", "AUTO_", "WATCH_", "FILAMENT_", "PHYSICAL_",
         "APPROVAL_", "BED", "MAX_Z", "ALERT_", "SERVE_", "DETECTOR", "MAINT_", "FARM_", "ORCASLICER", "PRUSASLICER",
         "SELFTEST_", "NOTIFY_")


class FarmBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pr_farm_"))
        env = {k: v for k, v in os.environ.items() if not k.startswith(CLEAR)}
        env.update({"PR_NO_DOTENV": "1", "PR_STATE_DIR": str(self.tmp / "state"), "PR_OWNER_INBOX": str(self.tmp / "inbox"),
                    "PR_FLEET_FILE": str(self.tmp / "printers.json"), "PR_FLEET_LOCAL_FILE": str(self.tmp / "printers.local.json"),
                    "PR_POLICY_FILE": str(self.tmp / "policy.json"), "PR_PROTOCOL_DOC": str(self.tmp / "PROTOCOL.md"),
                    "CREALITY_DATA_DIR": str(self.tmp / "nodata"), "APPDATA": str(self.tmp), "PR_WS_BACKOFF_S": "0.05",
                    "PR_FAKE_GUI_RUNNING": "none", "NOTIFY_TELEGRAM": "false", "SELFTEST_VERIFY_S": "6",
                    "FARM_START_GRACE_S": "0"})
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.printers: dict[str, FakePrinter] = {}

    def tearDown(self):
        for fp in self.printers.values():
            try:
                fp.stop()
            except Exception:
                pass
        self._env.stop()
        core.set_active_printer(None)
        core.set_actor(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, **env) -> dict:
        with mock.patch.dict(os.environ, {k: str(v) for k, v in env.items()}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                core.main([str(a) for a in args])
        core.set_active_printer(None)
        core.set_actor(None)
        txt = buf.getvalue().strip()
        try:
            return json.loads(txt)
        except ValueError:
            return json.loads(txt.splitlines()[-1])  # --jsonl: the summary is the last line

    def fleet(self, specs: list[dict]):
        """specs: [{"name":..., "material":..., ...}] -> starts one FakePrinter per entry, writes printers.json."""
        rows, local = [], {}
        for sp in specs:
            fp = FakePrinter().start()
            self.printers[sp["name"]] = fp
            row = {"model": "CR-10 SE", "nozzle_mm": 0.4, "bed_mm": [220, 220, 250], "groups": [], "tags": [], **sp,
                   "ws_port": fp.ws_port, "http_port": fp.http_port}
            rows.append(row)
            local[sp["name"]] = "127.0.0.1"
        (self.tmp / "printers.json").write_text(json.dumps({"printers": rows}))
        (self.tmp / "printers.local.json").write_text(json.dumps(local))
        return rows

    def policy(self, pol: dict):
        (self.tmp / "policy.json").write_text(json.dumps(pol))

    def gcode(self, name="part.gcode", minutes=10, grams=5) -> Path:
        p = self.tmp / name
        p.write_text(f"; estimated printing time (normal mode) = {minutes}m 0s\n; filament used [g] = {grams}\n"
                     "; filament_type = PETG\nM104 S240\nM140 S80\nG28\nG1 X10 Y10 Z0.2 F3000\nG1 X50 E5\n")
        return p

    def packets(self):
        d = self.tmp / "inbox"
        return sorted(d.glob("*.json")) if d.is_dir() else []


class _Srv:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class WebhookSink(_Srv):
    def __init__(self, fail_first: int = 0):
        sink = self
        self.calls: list = []
        self.fail_first = fail_first

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if sink.fail_first > 0:
                    sink.fail_first -= 1
                    self.send_response(503)
                    self.end_headers()
                    return
                sink.calls.append({"headers": dict(self.headers), "body": body})
                self.send_response(200)
                self.end_headers()
        super().__init__(H)


class FakeMoonraker(_Srv):
    def __init__(self):
        mk = self
        self.state = "standby"
        self.calls: list = []
        self.files: dict = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, d):
                b = json.dumps(d).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                mk.calls.append(("GET", self.path))
                if self.path.startswith("/printer/objects/query"):
                    return self._j({"result": {"status": {
                        "print_stats": {"state": mk.state, "filename": "a.gcode" if mk.state != "standby" else "", "print_duration": 12},
                        "extruder": {"temperature": 25.0, "target": 0}, "heater_bed": {"temperature": 24.0, "target": 0},
                        "virtual_sdcard": {"progress": 0.5 if mk.state == "printing" else 0}, "fan": {"speed": 0.5},
                        "webhooks": {"state": "ready"}}}})
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                mk.calls.append(("POST", self.path))
                if self.path.startswith("/printer/print/start"):
                    mk.state = "printing"
                elif self.path == "/printer/print/pause":
                    mk.state = "paused"
                elif self.path == "/printer/print/resume":
                    mk.state = "printing"
                elif self.path == "/printer/print/cancel":
                    mk.state = "cancelled"
                elif self.path == "/server/files/upload":
                    mk.files["upload"] = len(body)
                return self._j({"result": "ok"})
        super().__init__(H)


class FakeML(_Srv):
    def __init__(self, score: float):
        ml = self
        self.score = score

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                b = json.dumps({"detections": [["failure", ml.score, [1, 2, 3, 4]]]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b)
        super().__init__(H)
