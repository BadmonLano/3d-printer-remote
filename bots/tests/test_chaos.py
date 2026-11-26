"""Hardening + chaos suite (2.0): reconnect/backoff, websocket drops, mid-print disconnects, power-loss awareness,
stalled printers, garbage frames, idempotent requests, and a randomized soak. Fake printers only."""
from __future__ import annotations

import json
import random
import threading
import time

from farm_helpers import FarmBase, FakePrinter, core


class TestHardening(FarmBase):
    def setUp(self):
        super().setUp()
        self.fp = FakePrinter().start()
        self.printers["solo"] = self.fp
        import os
        os.environ.update({"PRINTER_IP": "127.0.0.1", "PRINTER_WS_PORT": str(self.fp.ws_port),
                           "PRINTER_HTTP_PORT": str(self.fp.http_port)})

    def test_backoff_delays_grow_and_cap(self):
        d = core.backoff_delays(6, 0.5, cap=4)
        self.assertEqual(len(d), 6)
        self.assertLess(d[0], d[2])
        self.assertTrue(all(x <= 4 * 1.15 for x in d))

    def test_status_survives_refused_connections(self):
        self.fp.refuse = 2  # first two connects die before the handshake
        o = self.cli("printer", "status")
        self.assertEqual(o["status"], "ok", o)
        self.assertGreaterEqual(self.fp.connections, 3)

    def test_unreachable_printer_fails_fast_with_json(self):
        t0 = time.monotonic()
        o = self.cli("printer", "status", PRINTER_WS_PORT="1", PR_WS_CONNECT_RETRIES="1", PR_STATUS_RETRIES="0")
        self.assertEqual(o["status"], "error")
        self.assertLess(time.monotonic() - t0, 15)

    def test_stalled_printer_times_out(self):
        self.fp.silent = True
        t0 = time.monotonic()
        o = self.cli("printer", "status", PR_STATUS_RETRIES="0")
        # handshake + initial push still arrive; the stall must not hang the CLI past its wait window
        self.assertLess(time.monotonic() - t0, 12)
        self.assertIn(o["status"], ("ok", "error"))

    def test_garbage_frames_are_ignored(self):
        self.fp.garbage = True
        o = self.cli("printer", "stream", "--seconds", "1.5", "--min-interval", "0.2")
        self.assertEqual(o["status"], "ok", o)
        self.assertGreater(o["updates"], 0)

    def test_stream_reconnects_after_drop(self):
        self.fp.status.update(state=1, deviceState=1, printFileName="egg.gcode", printProgress=40)
        threading.Timer(0.8, self.fp.drop_clients).start()
        o = self.cli("printer", "stream", "--seconds", "3.5", "--min-interval", "0.2")
        self.assertEqual(o["status"], "ok", o)
        self.assertGreaterEqual(o["reconnects"], 1, o)
        self.assertTrue(o["drops"])
        self.assertEqual(o["latest"]["state"], "printing")
        self.assertFalse([e for e in o["continuity_events"] if e["kind"] == "job_lost"])

    def test_power_loss_mid_print_is_reported(self):
        self.fp.status.update(state=1, deviceState=1, printFileName="egg.gcode", printProgress=40)
        threading.Timer(0.8, self.fp.power_loss).start()
        o = self.cli("printer", "stream", "--seconds", "3.5", "--min-interval", "0.2")
        kinds = {e["kind"] for e in o["continuity_events"]}
        self.assertIn("job_lost", kinds, o)
        self.assertIn("power_loss_resume_pending", kinds)
        self.assertEqual(o["latest"]["power_loss_recovery"], 1)

    def test_assess_continuity_cases(self):
        prev = {"printing": True, "idle": False, "file": "a.gcode", "progress_pct": 50}
        self.assertEqual(core.assess_continuity(prev, {"idle": True, "state": "complete", "progress_pct": 100})[0]["kind"],
                         "job_finished_while_away")
        self.assertEqual(core.assess_continuity(prev, {"idle": True, "state": "cancelled", "progress_pct": 50})[0]["kind"],
                         "job_lost")
        self.assertEqual(core.assess_continuity({"has_error": False}, {"has_error": True, "error": {"raw": 5}})[0]["kind"],
                         "printer_error")
        self.assertEqual(core.assess_continuity(None, {"power_loss_recovery": 0}), [])

    def test_resume_reheat_is_not_labelled_idle(self):
        n = core.normalize_status({"state": 0, "deviceState": 1, "nozzleTemp": "164", "targetNozzleTemp": 240})
        self.assertEqual(n["state"], "busy")
        self.assertFalse(n["idle"])
        self.assertTrue(n["printing"])

    def test_duplicate_requests_are_idempotent(self):
        a = self.cli("printer", "fan", "--pct", "40")
        b = self.cli("printer", "fan", "--pct", "40")
        self.assertEqual(a["request_id"], b["request_id"])
        self.assertTrue(b.get("duplicate_of_open_request"))
        c = self.cli("printer", "fan", "--pct", "41")
        self.assertNotEqual(a["request_id"], c["request_id"])

    def test_approval_is_single_use(self):
        r = self.cli("printer", "fan", "--pct", "30")
        ok = self.cli("approve", r["request_id"], "--yes", "--quote", "yes")
        self.assertEqual(ok["status"], "ok", ok)
        again = self.cli("approve", r["request_id"], "--yes", "--quote", "yes")
        self.assertEqual(again["status"], "error")
        self.assertIn("already answered", again["errors"][0])
        fans = [m for m in self.fp.ws_messages if m.get("params", {}).get("gcodeCmd", "").startswith("M106")]
        self.assertEqual(len(fans), 1)

    def test_start_refused_when_already_printing(self):
        self.fp.uploads["a.gcode"] = b"G28"
        r = self.cli("printer", "start", "a.gcode")
        self.fp.status.update(state=1, deviceState=1)
        o = self.cli("approve", r["request_id"], "--yes", "--quote", "yes")
        self.assertEqual(o["status"], "preflight_failed", o)
        self.assertFalse([m for m in self.fp.ws_messages if "opGcodeFile" in m.get("params", {})])

    def test_mid_print_disconnect_during_pause_request(self):
        self.fp.status.update(state=1, deviceState=1)
        r = self.cli("printer", "pause")
        self.fp.refuse = 1  # one refused connect during preflight: retried, still works
        o = self.cli("approve", r["request_id"], "--yes", "--quote", "pause it")
        self.assertEqual(o["status"], "ok", o)
        self.assertEqual(self.fp.status["state"], 5)
        r2 = self.cli("printer", "resume")
        o2 = self.cli("approve", r2["request_id"], "--yes", "--quote", "go")
        self.assertEqual(o2["status"], "ok", o2)
        self.assertEqual(self.fp.status["state"], 1)

    def test_soak_random_chaos_never_hangs(self):
        rnd = random.Random(7)
        t_end = time.monotonic() + 8
        n, ok = 0, 0
        while time.monotonic() < t_end:
            ev = rnd.choice(["none", "none", "refuse", "drop", "garbage"])
            self.fp.garbage = ev == "garbage"
            if ev == "refuse":
                self.fp.refuse = 1
            if ev == "drop":
                threading.Timer(0.05, self.fp.drop_clients).start()
            t0 = time.monotonic()
            o = self.cli("printer", "status", PR_WS_CONNECT_RETRIES="2")
            self.assertLess(time.monotonic() - t0, 10)
            self.assertIn(o["status"], ("ok", "error"))
            n += 1
            ok += o["status"] == "ok"
        self.assertGreaterEqual(n, 3)
        self.assertGreaterEqual(ok / n, 0.8, f"{ok}/{n} ok under chaos")
