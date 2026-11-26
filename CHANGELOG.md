# Changelog

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
