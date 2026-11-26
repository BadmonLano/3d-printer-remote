"""Slice preset origin: Creality Print 7.2's CLI rejects a flattened user process with
"process not compatible with printer" unless the printer is from=system and user presets keep
inherits=<system root>. Found on a real CR-10 SE slice (v1.2.2)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_printer_remote import Base, make_plain_3mf  # noqa: E402


class TestSliceOrigin(Base):
    def _files(self, o):
        cp = o["command_preview"]
        sets = cp[cp.index(next(x for x in cp if x.startswith("--load") and "settings" in x)) + 1].split(";")
        fils = cp[cp.index(next(x for x in cp if x.startswith("--load") and "filaments" in x)) + 1].split(";")
        return [json.loads(Path(p).read_text(encoding="utf-8")) for p in sets], \
               [json.loads(Path(p).read_text(encoding="utf-8")) for p in fils]

    def test_user_process_keeps_system_root_and_printer_is_system(self):
        m = make_plain_3mf(self.tmp / "plate.3mf")
        o = self.run_cli("slice", str(m), "--process", "Big gap", "--keep-temp")
        self.assertEqual(o["status"], "ok", o)
        (machine, process), (fil,) = self._files(o)
        self.assertEqual(machine["from"], "system")
        self.assertNotIn("inherits", machine)
        self.assertEqual(process["from"], "User")
        self.assertEqual(process["inherits"], "0.20mm Standard @Creality CR-10 SE 0.4 nozzle")
        self.assertEqual(process["enable_support"], "1")
        self.assertEqual(fil["from"], "system")

    def test_cloned_user_filament_inherits_system(self):
        c = self.run_cli("presets", "clone", "CR-PETG @Creality CR-10 SE 0.4 nozzle", "Toy PETG", "--set", "nozzle_temperature=240")
        self.assertEqual(c["status"], "ok", c)
        m = make_plain_3mf(self.tmp / "plate.3mf")
        o = self.run_cli("slice", str(m), "--filament", "Toy PETG", "--keep-temp")
        self.assertEqual(o["status"], "ok", o)
        _, (fil,) = self._files(o)
        self.assertEqual(fil["from"], "User")
        self.assertEqual(fil["inherits"], "CR-PETG @Creality CR-10 SE 0.4 nozzle")


if __name__ == "__main__":
    unittest.main()
