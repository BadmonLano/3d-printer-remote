# Dependency map

- `bots/printer_remote.py` - the one CLI entry point and core (stdlib; Pillow/numpy optional for camera).
  - reads: Creality Print exe + `%APPDATA%\Creality\Creality Print\7.0\{system,user}`, `deviceInfo.json`, `.env`
  - writes: `bots/state/` (gcode, logs, backups, pending/approvals, snapshots, printer_latest.json, watch_state.json),
    `owner_inbox/`, user presets (clone / set, with backup)
  - talks to: printer ports 80 + 9999 on the LAN, camera URL, Telegram API (optional)
  - loads the farm modules below lazily (`_mod`) and registers their subcommands; they import it back as `core`.
- Farm modules (2.0), all under `bots/`, none is an entry point:
  - `pr_fleet.py` - `printers.json`/`.yaml` + `printers.local.json`, `--printer` targeting, fleet status/stream, discovery.
  - `pr_farm.py` - farm queue (`bots/state/farm_queue.json`), routing, dispatch through `request_gated`, sync, failover, intake.
  - `pr_policy.py` - `policy.json`, autonomy levels, kill switch (`bots/state/killswitch.json`), rate limits, audit (`bots/state/audit.jsonl`).
  - `pr_security.py` - API tokens (`bots/state/tokens.json`, hashed) and roles.
  - `pr_alerts.py` - alert events -> owner_inbox packet, Telegram (core adapter), webhooks.
  - `pr_service.py` - `serve` daemon, supervisor, pid file (`bots/state/serve.pid`), dashboard, REST API.
  - `pr_ops.py` - per-printer maintenance (`bots/state/fleet_maint.json`), spool locations (inventory store), analytics.
  - `pr_plugins.py` - slicer backends, failure detectors, recovery playbooks, Moonraker backend.
  - `pr_selftest.py` - `selftest hardware`, results in `bots/state/selftest_results.json` + `docs/PROTOCOL.md`.
- Optional: PyYAML (only for `printers.yaml`), an external ML detector server (`DETECTOR_ML_URL`), webhook endpoints
  (`ALERT_WEBHOOK_URLS`), OrcaSlicer / PrusaSlicer executables.
- Tests (`bots/tests/`):
  - `fake_printer.py` - fake HTTP upload + websocket printer with live pushes and chaos controls (refuse, drop, stall,
    garbage frames, power loss).
  - `fake_slicer.py` - fake CrealityPrint.exe.
  - `farm_helpers.py` - in-process CLI runner, multi-printer fleets, fake Moonraker, webhook sink, fake ML server.
  - `test_printer_remote.py`, `test_features_v11.py`, `test_slice_origin.py` - 1.x suite (subprocess CLI).
  - `test_chaos.py`, `test_fleet.py`, `test_farm.py`, `test_policy.py`, `test_service.py`, `test_ops.py` - 2.0 suite.

## Why the split (2.0)
`printer_remote.py` was ~3,200 lines before 2.0, and the farm features add ~3,000 more. Keeping them in separate
modules keeps the core readable and reviewable, lets a single-printer install ignore them (they load only when a farm
command or config is used), and keeps the one-entry rule: every command still runs through
`python bots/printer_remote.py ...` and prints one JSON object. The modules use `core.*` for every printer write, so
the approval gate, preflight, kill switch and audit cannot be bypassed from a module.
