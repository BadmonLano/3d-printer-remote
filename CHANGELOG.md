# Changelog

## 1.2.2
- Docs: the test command is `python -m unittest discover -s bots/tests -v` (README, SETUP, CONTRIBUTING, SKILL).
  `python -m pytest bots/tests` failed on a fresh clone because pytest wasn't installed; it's now optional through the
  new `requirements-dev.txt`.
- Fix: `slice` with clone/user presets failed on Creality Print 7.2 ("process not compatible with printer"). Settings
  files now keep `from: system` for system presets and `inherits: <system root>` for user ones. First real CLI slice
  done (a test plate, GUI open, separate output dir). Test: `bots/tests/test_slice_origin.py`.
- Skill: new "Print quality / settings checklist" (published CR-PETG / CR-10 SE values with sources, kids-toy rules,
  CLI gotchas, what the SE start sequence looks like, live progress via `printer stream`).
- First real print started through the approval flow (owner yes in chat, preflight passed; no camera, so no bed check).

## 1.2.1
- `printer cooldown --urgent --reason "..." [--fan]`: heaters off right away (nozzle 0, bed 0, optional part fan max),
  with no owner yes. It's for urgent cases only (jam, burning smell, runaway or printer error, crash on camera, the owner
  says stop, a hot nozzle left after a failure). It writes an owner_inbox packet, a `safety_log` entry and a note on the
  open history job, all with ET time. Plain `printer cooldown` still needs the owner's yes. `--urgent` is refused on
  every other command.
- Urgent cooldown ignores `PHYSICAL_ACTIONS_ENABLED=false` on purpose: it only removes heat, and the kill switch blocks
  actions that add risk.
- Filament-move failures (heat timeout, printer error, lost connection) now use the same urgent cooldown. The new
  `AUTO_COOLDOWN_ON_ANOMALY` (default false) lets the watch call it on thermal anomalies or printer errors.
- Docs: the real printer LAN address is out of tracked files (placeholder `<PRINTER_IP>`; test fixtures use
  192.0.2.10 from the documentation range).
- Preheat follows the material spec (owner OK, 1:41-1:42 PM ET): `--material` or `FILAMENT_MATERIAL` gives PLA 210,
  PETG 240, ABS 245, ASA 250, TPU 225 C, and PETG 240 when nothing is given. `--purge` (or `--temp 300`) is the explicit
  300 C hardened-filament purge. `FILAMENT_PREHEAT_C` stays as a personal override for a local `.env`.
- One nozzle ceiling: 300 C for filament moves and `printer heat` (heat was 0-260). `FILAMENT_MAX_C` can lower it but
  not raise it, and anything above is rejected. `printer heat` above 260 C warns that it's purge territory.
- Tests ignore the local `.env` (`PR_NO_DOTENV=1`), so the owner's personal settings can't change test results.

## 1.2.0

> Not yet hardware-verified on a CR-10 SE beyond read-only data and one owner-approved retract (see docs/PROTOCOL.md).

- New gated filament commands: `filament extrude --mm N`, `filament retract --mm N`, `filament load`, `filament unload`.
  Each preheats to `FILAMENT_PREHEAT_C` (300 C by default, max 320), waits until the nozzle is within
  `FILAMENT_TEMP_TOL_C` (3), soaks `FILAMENT_SOAK_S` (5 s), sends `M83`, `G1 E… F…` in pieces of 45 mm or less (Klipper's
  50 mm extrude-only limit), then `M82`, waits for the move and sets the nozzle to 0 (`--hold` keeps it hot). Any error
  or heat timeout (`FILAMENT_HEAT_TIMEOUT_S`, 600) turns the nozzle off and writes an owner_inbox packet. Limits:
  100 mm per extrude or retract, 120 mm per load or unload, 0.5-10 mm/s. Refused while printing unless paused; preflight
  also checks the preheat against the printer's max.
- New gated `printer cooldown` (nozzle and bed to 0).
- The result includes a temperature timeline, the time to reach temperature and every gcode sent, with timings.

## 1.1.0

> Not yet hardware-verified on a CR-10 SE for uploads and physical actions. Read-only live data was checked on a
> real CR-10 SE (field names recorded in docs/PROTOCOL.md).

- **Owner approval replaces confirm codes.** Physical actions return `awaiting_owner` with a summary (part, time,
  filament, snapshot). The owner says yes or no in chat; `approve <id> --yes|--no --by --quote` records it (ET time,
  source, owner's words). On yes an automatic `preflight` checks the kill switch, printer online, state, errors, temps,
  file on the printer, filament on hand and the camera bed check, and runs the action only if it passes. Requests are
  single use and expire (15 min). New: `preflight <id>`; `confirm` and the code machinery are gone.
- `PHYSICAL_ACTIONS_ENABLED` is checked inside `execute_physical`, so `AUTO_PAUSE_ON_ANOMALY` can't bypass it.
- New gated actions: `printer move` (bed-bounded absolute, relative max 50 mm, refused mid-print), `printer home`,
  `printer fan`, motion/heat `printer gcode`. Read-only gcode queries (M105, M114...) are free.
- Protocol: `wsslicer` subprotocol, `ReqPrinterPara`, heat via `gcodeCmd` M104/M140; new reads `printer
  info/files/history/mesh/objects/timelapses/config/ota/materials`, `printer light`, and upload checks the file landed.
- Jobs: `queue` with filament-swap prompts, `history` + stats + cost, `filament` inventory, `plates by-color`,
  `recommend`, `maint` with jam log, `library`, `gcode inspect`, `timelapse build`, `health`.
- Watch job events: first-layer photo, ETA notice, paused notice, done photo with "clear the bed" and the next queue
  item.
- `printer probe` reports `error` when every port times out.
- Real CR-10 SE findings: file list comes as `retGcodeFileInfo` (string format, now parsed); query replies drop the
  status keys the printer pushes with every message.
- Personal data removed from the public tree. The owner-specific agent copy (`jordan/`) is gitignored; `skills/` holds
  a generic skill. "AI agent" wording; Linux setup examples.
- Tests close their sockets; 47 offline tests.

## 1.0.0
- First release of 3D Printer Remote for Creality Print 7.2 + Creality CR-10 SE.
- Slicer: `detect`, `presets list/show/clone/set` (inheritance resolved; system presets never edited; user presets
  backed up first), `slice` (3MF/STL, CR-10 SE + PETG defaults for plain files, overrides, 7.2 underscore flags with
  automatic dash fallback, 7.3 `--cli --need-gcode-file`, GUI-open guard, gcode time/grams/layers), `open`,
  `split-check`.
- Printer LAN: `printer discover/probe/status/stream/latest/upload`. Live data from ws 9999 normalized to stable
  JSON, `bots/state/printer_latest.json`, rolling daily log.
- Owner gate: `printer start/pause/resume/cancel/heat` -> single-use 4-char code in an owner_inbox packet ->
  `confirm <CODE>`. Kill switch `PHYSICAL_ACTIONS_ENABLED`.
- Camera: timestamped snapshots (ET), empty-bed reference, advisory bed-clear check attached to every start request.
- Malfunction watch: frame jump, camera and progress stalls, temp runaway / drop / heating without target, printer
  errors -> owner_inbox alert + optional Telegram photo; pause request goes through the gate (auto-pause off).
- Offline test suite (fake slicer, fake printer with live pushes, fake camera, fake Telegram).
