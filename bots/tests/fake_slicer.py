"""Fake CrealityPrint.exe for offline tests. Mimics the CLI contract we rely on:
--slice N --outputdir DIR --logfile F [--load_settings a;b --load_filaments c] model
Writes plate_1.gcode with an Orca-style stats footer.
FAKE_SLICER_DIALECT=dash makes underscore flags fail with "Invalid option" (like some 7.2 builds).
FAKE_SLICER_CRASH=1 exits 3221225477 with no gcode (issue #574 style).
"""
import os
import sys
from pathlib import Path

args = sys.argv[1:]
dialect = os.environ.get("FAKE_SLICER_DIALECT", "underscore")
if os.environ.get("FAKE_SLICER_CRASH"):
    sys.exit(3221225477 % 256 if os.name != "nt" else 3221225477)
flags = [a for a in args if a.startswith("--")]
bad = [f for f in flags if ("_" in f if dialect == "dash" else "-" in f[2:])]
if bad:
    print(f"Invalid option {bad[0]}")
    sys.exit(2)
def val(name):
    for f in (f"--{name}", f"--{name.replace('_', '-')}"):
        if f in args:
            return args[args.index(f) + 1]
out = Path(val("outputdir"))
log = val("logfile")
model = args[-1]
if log:
    Path(log).write_text(f"[info] fake slicing {model}\n[error] this is a normal message tagged error\n")
if not Path(model).is_file():
    sys.exit(1)
out.mkdir(parents=True, exist_ok=True)
settings = val("load_settings") or ""
(out / "plate_1.gcode").write_text(
    "; HEADER_BLOCK_START\n; model printing time: 1h 2m 3s; total estimated time: 1h 5m 0s\n; total layer number: 120\n"
    "; HEADER_BLOCK_END\nG28\nG1 X10 Y10\n"
    "; filament used [mm] = 1234.50\n; filament used [g] = 12.34\n"
    f"; fake_load_settings = {settings}\n")
sys.exit(0)
