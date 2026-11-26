# Contributing

This code can heat and move a real printer once the owner approves, so the bar is "safe and tested" before "clever".

## Ground rules

- **Never commit secrets.** No tokens, `.env`, IPs of real printers, snapshots of someone's home.
- **No personal data** in code, skills, fixtures or docs.
- **Keep it self-contained.** One main file (`bots/printer_remote.py`); only truly separate engines get their own file.
- **The approval gate is not up for loosening** in a PR: `request_gated`, `cmd_approve`, `preflight`,
  `execute_physical` and their tests. No path may start, pause, resume, cancel, heat, home, move or set the fan without
  an owner yes plus a passing preflight, except the documented opt-in `AUTO_PAUSE_ON_ANOMALY` (pause only, still
  blocked by `PHYSICAL_ACTIONS_ENABLED=false`). New preflight checks are welcome; removing one needs a reason.
- **No network in tests.** Use `bots/tests/fake_printer.py`, `fake_slicer.py` and the mocks in the test file.

## Before you open a PR

```
python -m unittest discover -s bots/tests -v
python -m py_compile bots/printer_remote.py
```

Optional: `pip install -r requirements-dev.txt` adds pytest, and then `python -m pytest bots/tests` works too.

Say what changed, why, and how you tested it (and on which printer / Creality Print version if you used hardware).
