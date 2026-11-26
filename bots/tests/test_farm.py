"""Shared farm queue + routing (2.0): material/color/nozzle/bed fit, priority, dependencies, N copies across the
farm, batch affinity, failover, order intake, and dispatch through the approval gate on multiple fake printers."""
from __future__ import annotations

import json

from farm_helpers import FarmBase, core

farm = core._mod("pr_farm")


def row(name, material="PETG", color="white", nozzle=0.4, bed=(220, 220, 250), available=True, **kw):
    return {"name": name, "material": material, "color": color, "nozzle_mm": nozzle, "bed_mm": list(bed),
            "available": available, "unavailable_reasons": [] if available else ["busy"], "reachable": True, **kw}


class TestRouting(FarmBase):
    def jobs(self, *specs):
        jobs = []
        for sp in specs:
            farm.add_job(jobs, "/nowhere/part.gcode", **sp)
        return jobs

    def test_material_color_nozzle_and_bed_fit(self):
        jobs = self.jobs({"material": "PLA", "color": "orange"}, {"material": "PETG", "color": "white", "size": "250x100x50"},
                         {"material": "PETG", "color": "white", "nozzle": 0.6}, {"material": "PETG", "color": "white"})
        p = farm.plan(jobs, [row("se1"), row("big", bed=(300, 300, 400), nozzle=0.6)])
        got = {a["printer"]: a["job"] for a in p["assignments"]}
        self.assertEqual(got["se1"], jobs[3]["id"])           # only the plain white PETG fits se1
        self.assertIn(got["big"], (jobs[1]["id"], jobs[2]["id"]))  # only the big 0.6 printer takes these
        unrouted = {u["job"]: u["why"] for u in p["unrouted"]}
        self.assertIn(jobs[0]["id"], unrouted)                # nobody has orange PLA loaded
        self.assertTrue(any("NO printer" in w for w in unrouted[jobs[0]["id"]]))

    def test_scarce_job_gets_its_only_printer(self):
        jobs = self.jobs({"material": "PETG", "color": "white", "name": "flex"},
                         {"material": "PETG", "color": "white", "name": "tall", "size": "100x100x300"})
        p = farm.plan(jobs, [row("big", bed=(300, 300, 400)), row("se1")])
        got = {a["printer"]: a["name"] for a in p["assignments"]}
        self.assertEqual(got, {"big": "tall", "se1": "flex"})

    def test_bed_fit_allows_rotation(self):
        jobs = self.jobs({"material": "PETG", "color": "white", "size": "100x215x10"})
        p = farm.plan(jobs, [row("se1", bed=(220, 200, 250))])
        self.assertEqual(len(p["assignments"]), 1)

    def test_priority_then_fifo(self):
        jobs = self.jobs({"material": "PETG", "color": "white", "name": "low"},
                         {"material": "PETG", "color": "white", "name": "urgent", "priority": 5})
        p = farm.plan(jobs, [row("se1")])
        self.assertEqual(p["assignments"][0]["name"], "urgent")

    def test_dependencies_wait(self):
        jobs = self.jobs({"material": "PETG", "color": "white", "name": "base"})
        farm.add_job(jobs, "/nowhere/lid.gcode", material="PETG", color="white", name="lid", depends_on=[jobs[0]["id"]], priority=9)
        p = farm.plan(jobs, [row("se1"), row("se2")])
        self.assertEqual([a["name"] for a in p["assignments"]], ["base"])
        self.assertIn(jobs[1]["id"], p["waiting_on_dependencies"])
        jobs[0]["status"] = "done"
        p = farm.plan(jobs, [row("se1")])
        self.assertEqual(p["assignments"][0]["name"], "lid")

    def test_copies_spread_across_the_farm(self):
        jobs = self.jobs({"material": "PETG", "color": "white", "copies": 3, "name": "egg"})
        self.assertEqual(len(jobs), 3)
        p = farm.plan(jobs, [row("se1"), row("se2"), row("se3", available=False)])
        self.assertEqual(sorted(a["printer"] for a in p["assignments"]), ["se1", "se2"])
        self.assertEqual(len({a["job"] for a in p["assignments"]}), 2)

    def test_batch_affinity(self):
        jobs = self.jobs({"material": "PETG", "color": "white", "batch": "A"}, {"material": "PETG", "color": "white", "batch": "B"})
        prior = {"id": "old", "status": "done", "printer": "se1", "batch": "B", "priority": 0, "created_epoch": 0}
        jobs.insert(0, prior)
        p = farm.plan(jobs, [row("se1")])
        self.assertEqual(p["assignments"][0]["job"], jobs[2]["id"])
        self.assertTrue(p["assignments"][0]["batch_affinity"])

    def test_failover_excludes_failed_printer(self):
        jobs = self.jobs({"material": "PETG", "color": "white"})
        jobs[0].update(status="printing", printer="se1", attempts=1)
        r = farm.mark_finished(jobs, jobs[0]["id"], False, "spaghetti")
        self.assertEqual(r["status"], "requeued")
        p = farm.plan(jobs, [row("se1"), row("se2")])
        self.assertEqual(p["assignments"][0]["printer"], "se2")
        jobs[0].update(status="printing", printer="se2", attempts=2)
        self.assertEqual(farm.mark_finished(jobs, jobs[0]["id"], False, "again")["status"], "failed")

    def test_missing_dependency_rejected(self):
        with self.assertRaises(core.Fail):
            farm.add_job([], "/x.gcode", depends_on=["fj-nope"])


class TestFarmDispatch(FarmBase):
    def setUp(self):
        super().setUp()
        self.fleet([{"name": "se1", "material": "PETG", "color": "white"},
                    {"name": "se2", "material": "PETG", "color": "white"}])

    def test_end_to_end_owner_yes_dispatch_sync_failover(self):
        g = self.gcode("egg.gcode")
        a = self.cli("farm", "add", g, "--material", "PETG", "--color", "white", "--copies", "2")
        self.assertEqual(len(a["added"]), 2)
        d = self.cli("farm", "dispatch")
        self.assertEqual(d["status"], "ok", d)
        self.assertEqual(sorted(x["printer"] for x in d["dispatched"]), ["se1", "se2"])
        self.assertTrue(all(x["status"] == "awaiting_owner" for x in d["dispatched"]))
        # nothing started yet: owner_yes is the default
        for fp in self.printers.values():
            self.assertFalse([m for m in fp.ws_messages if "opGcodeFile" in m.get("params", {})])
            self.assertEqual(len(fp.uploads), 1)
        rid = d["dispatched"][0]["request_id"]
        o = self.cli("approve", rid, "--yes", "--quote", "yes print it")
        self.assertEqual(o["status"], "ok", o)
        jobs = {j["id"]: j for j in self.cli("farm", "list")["jobs"]}
        started = [j for j in jobs.values() if j["status"] == "printing"]
        self.assertEqual(len(started), 1)
        pn = started[0]["printer"]
        # printer fails mid-job -> sync marks failed -> failover requeues away from that printer
        self.printers[pn].status.update(state=4, deviceState=0, printProgress=30)
        s = self.cli("farm", "sync")
        self.assertEqual(s["changes"][0]["status"], "requeued", s)
        j = next(x for x in self.cli("farm", "list")["jobs"] if x["id"] == started[0]["id"])
        self.assertIn(pn, j["excluded_printers"])
        hist = self.cli("history", "list")["history"]
        self.assertEqual(hist[-1]["printer"], pn)
        self.assertEqual(hist[-1]["farm_job_id"], started[0]["id"])

    def test_sync_done(self):
        g = self.gcode()
        self.cli("farm", "add", g, "--material", "PETG", "--color", "white")
        d = self.cli("farm", "dispatch", "--printer", "se1")
        self.cli("approve", d["dispatched"][0]["request_id"], "--yes", "--quote", "y")
        self.printers["se1"].status.update(state=2, deviceState=0, printProgress=100)
        s = self.cli("farm", "sync")
        self.assertEqual(s["changes"][0]["status"], "done")

    def test_declined_job_is_held(self):
        self.cli("farm", "add", self.gcode(), "--material", "PETG", "--color", "white")
        d = self.cli("farm", "dispatch", "--printer", "se1")
        self.cli("approve", d["dispatched"][0]["request_id"], "--no", "--quote", "not now")
        j = self.cli("farm", "list")["jobs"][0]
        self.assertEqual(j["status"], "held")
        self.assertEqual(self.cli("farm", "dispatch")["dispatched"], [])
        self.cli("farm", "release", j["id"])
        self.assertEqual(self.cli("farm", "list")["jobs"][0]["status"], "queued")

    def test_dispatch_dry_run_and_plan_send_nothing(self):
        self.cli("farm", "add", self.gcode(), "--material", "PETG", "--color", "white")
        p = self.cli("farm", "plan")
        self.assertEqual(len(p["assignments"]), 1)
        d = self.cli("farm", "dispatch", "--dry-run")
        self.assertTrue(d["dispatched"][0]["dry_run"])
        self.assertTrue(all(not fp.uploads for fp in self.printers.values()))

    def test_intake_csv_idempotent(self):
        g = self.gcode("lid.gcode")
        csvp = self.tmp / "orders.csv"
        csvp.write_text("order_id,file,material,color,copies,priority\nA100,lid.gcode,PETG,white,2,3\nA101,lid.gcode,PLA,orange,1,0\n")
        o = self.cli("farm", "intake", csvp)
        self.assertEqual(o["count"], 3, o)
        again = self.cli("farm", "intake", csvp)
        self.assertEqual(again["count"], 0)
        jobs = self.cli("farm", "list")["jobs"]
        self.assertEqual({j["order_id"] for j in jobs}, {"A100", "A101"})
        self.assertEqual(max(j["priority"] for j in jobs), 3)

    def test_intake_json(self):
        self.gcode("a.gcode")
        p = self.tmp / "orders.json"
        p.write_text(json.dumps({"orders": [{"order_id": "J1", "file": "a.gcode", "material": "PETG", "color": "white"}]}))
        self.assertEqual(self.cli("farm", "intake", p)["count"], 1)
