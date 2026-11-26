"""Autonomy policies, kill switches, rate limits and audit (2.0). Default stays owner yes + preflight."""
from __future__ import annotations

from farm_helpers import FarmBase, core

pol = core._mod("pr_policy")


class TestDecide(FarmBase):
    def test_default_is_owner_yes(self):
        self.assertEqual(pol.decide("start", {})["level"], "owner_yes")

    def test_resolution_order_and_limits(self):
        self.fleet([{"name": "se1", "groups": ["garage", "lab"]}])
        self.policy({"default": "owner_yes", "groups": {"garage": "auto_after_preflight", "lab": "notify_only"},
                     "printers": {}, "profiles": {"toy": {"level": "auto_after_preflight", "materials": ["PETG"]}}})
        # safest group wins
        self.assertEqual(pol.decide("start", {"printer": "se1"})["level"], "notify_only")
        # profile cannot escape a notify_only lock
        self.assertEqual(pol.decide("start", {"printer": "se1", "profile": "toy", "material": "PETG"})["level"], "notify_only")
        self.policy({"printers": {"se1": "owner_yes"}, "profiles": {"toy": {"level": "auto_after_preflight", "materials": ["PETG"]}}})
        self.assertEqual(pol.decide("start", {"printer": "se1", "profile": "toy", "material": "PETG"})["level"], "auto_after_preflight")
        self.assertEqual(pol.decide("start", {"printer": "se1", "profile": "toy", "material": "PLA"})["level"], "owner_yes")

    def test_dangerous_actions_never_auto(self):
        self.policy({"default": "auto_after_preflight", "auto_actions": ["start", "pause"]})
        for act in ("heat", "move", "home", "fan", "gcode", "extrude", "load", "cancel"):
            self.assertEqual(pol.decide(act, {})["level"], "owner_yes", act)
        self.assertEqual(pol.decide("pause", {})["level"], "auto_after_preflight")
        self.assertEqual(pol.decide("resume", {})["level"], "owner_yes")  # not listed in auto_actions
        self.assertEqual(pol.decide("start", {"selftest_step": "start"})["level"], "owner_yes")

    def test_policy_set_refuses_never_auto_actions(self):
        o = self.cli("policy", "set", "--level", "auto_after_preflight", "--auto-actions", "start,heat")
        self.assertEqual(o["status"], "error")
        o = self.cli("policy", "set", "--level", "owner_yes", "--printer", "se1", PR_ACTOR_ROLE="operator")
        self.assertEqual(o["status"], "error")
        self.assertIn("owner", o["errors"][0])


class TestPolicyOnPrinters(FarmBase):
    def setUp(self):
        super().setUp()
        self.fleet([{"name": "se1", "material": "PETG", "color": "white"}])
        self.fp = self.printers["se1"]
        self.fp.uploads["toy.gcode"] = b"G28"

    def starts(self):
        return [m for m in self.fp.ws_messages if "opGcodeFile" in m.get("params", {})]

    def test_auto_after_preflight_starts_and_audits(self):
        self.policy({"profiles": {"toy": {"level": "auto_after_preflight", "materials": ["PETG"]}}})
        self.cli("farm", "add", "toy.gcode", "--material", "PETG", "--color", "white", "--profile", "toy")
        d = self.cli("farm", "dispatch")
        self.assertEqual(d["dispatched"][0]["status"], "ok", d)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.cli("farm", "list")["jobs"][0]["status"], "printing")
        ev = [r["event"] for r in self.cli("audit")["audit"]]
        self.assertIn("auto_executed", ev)

    def test_auto_still_needs_preflight(self):
        self.policy({"default": "auto_after_preflight"})
        self.fp.status.update(state=1, deviceState=1)  # busy: preflight fails
        o = self.cli("printer", "start", "toy.gcode", "--printer", "se1")
        self.assertEqual(o["status"], "preflight_failed", o)
        self.assertEqual(self.starts(), [])

    def test_notify_only_sends_nothing(self):
        self.policy({"printers": {"se1": "notify_only"}})
        o = self.cli("printer", "pause", "--printer", "se1")
        self.assertEqual(o["status"], "notified")
        self.assertFalse([m for m in self.fp.ws_messages if "pause" in m.get("params", {})])
        self.assertTrue(any("notice" in p.name for p in self.packets()))

    def test_kill_switch_blocks_everything_but_urgent_cooldown(self):
        self.cli("policy", "kill", "--reason", "smoke test")
        o = self.cli("printer", "fan", "--pct", "50", "--printer", "se1")
        self.assertEqual(o["status"], "error")
        self.assertIn("kill switch", o["errors"][0])
        u = self.cli("printer", "cooldown", "--urgent", "--reason", "smell", "--printer", "se1")
        self.assertEqual(u["status"], "ok", u)
        o = self.cli("policy", "unkill", PR_ACTOR_ROLE="operator")
        self.assertEqual(o["status"], "error")
        self.cli("policy", "unkill")
        self.assertEqual(self.cli("printer", "fan", "--pct", "50", "--printer", "se1")["status"], "awaiting_owner")

    def test_kill_after_request_blocks_approval(self):
        r = self.cli("printer", "fan", "--pct", "50", "--printer", "se1")
        self.cli("policy", "kill", "--printer", "se1", "--reason", "jam")
        o = self.cli("approve", r["request_id"], "--yes", "--quote", "yes")
        self.assertEqual(o["status"], "preflight_failed")
        self.assertIn("kill_switch_off", o["preflight"]["failed"])

    def test_rate_limit(self):
        self.policy({"default": "auto_after_preflight", "auto_actions": ["pause", "resume"],
                     "rate_limits": {"auto_per_hour": 10, "auto_per_printer_per_hour": 2}})
        self.fp.status.update(state=1, deviceState=1)
        self.assertEqual(self.cli("printer", "pause", "--printer", "se1")["status"], "ok")
        self.assertEqual(self.cli("printer", "resume", "--printer", "se1")["status"], "ok")
        o = self.cli("printer", "pause", "--printer", "se1")
        self.assertEqual(o["status"], "error", o)
        self.assertIn("rate limit", o["errors"][0])

    def test_audit_records_actor(self):
        self.cli("--actor", "goat", "printer", "fan", "--pct", "20", "--printer", "se1")
        rows = self.cli("audit", "--by", "goat")["audit"]
        self.assertEqual(rows[-1]["event"], "request_created")
        self.assertEqual(rows[-1]["printer"], "se1")
