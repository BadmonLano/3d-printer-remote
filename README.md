<p align="center"><img src="assets/printer_remote_logo.png" alt="3D Printer Remote" width="640"></p>

# 3D Printer Remote

> **Not yet hardware-verified on a CR-10 SE** for uploads and most physical actions (start, pause, heat, move...). The
> slicer side was checked on a real Creality Print 7.2 install, and read-only live data (status, files, history,
> config, mesh) was checked on a real CR-10 SE, plus one owner-approved `filament retract`. See
> [docs/LIMITS.md](docs/LIMITS.md).

A bridge between your AI agent and **Creality Print 7.2** plus a **Creality CR-10 SE** (Creality OS, Klipper based,
built-in Wi-Fi). Your AI agent (Grok Bot, Cursor, a custom agent...) runs one Python CLI and reads its JSON.

- **Slicer:** detect Creality Print; list, show, clone and edit presets (only copies, or user presets backed up
  first); slice 3MF/STL from the command line with overrides; read time, grams and cost from the gcode; open a file in
  the GUI; check bed fit and piece count; split a plate by color; recommend settings for the part.
- **Printer (LAN):** live data over the printer's websocket (`ws://<ip>:9999`): temps, state, progress, layers, times,
  fans, position and errors. It also lists files, history, mesh, timelapses and config, uploads gcode and checks the
  file landed, and runs read-only gcode queries (M105, M114...).
- **Jobs:** a print queue with filament-swap prompts, history and stats with cost, filament inventory, a maintenance
  tracker with a jam log, a file library, a gcode inspector, and a health report.
- **Camera (optional):** ET-timestamped snapshots, a bed-clear check, a first-layer photo, a done photo with a
  "clear the bed" reminder, timelapse build, and a malfunction watch with owner_inbox / Telegram alerts.
- **Filament:** extrude, retract, load and unload. Each one preheats for the material (`--material PLA` 210, PETG 240
  by default, ABS 245, ASA 250, TPU 225). `--purge` or `--temp 300` is the explicit 300 C hardened-filament purge.
  Then it moves the filament slowly and turns the nozzle off (or `--hold`). The hard cap is 300 C (the printer reports
  320 C; we stay under the stock hotend rating). Limits: 100 mm per extrude or retract, 120 mm per load or unload.
- **Owner approval:** start, pause, resume, cancel, heat, home, move, fan, cooldown, filament moves and motion/heat gcode never run on the first
  call. They create a request (`awaiting_owner`) with a summary. **The owner answers YES or NO in chat.** On yes the
  agent runs `approve <id> --yes`, an automatic **preflight** checks the printer, and the action runs only if preflight
  passes.

The core is the Python 3.12 standard library. Pillow and numpy are optional (camera features).

## Quick start

Windows:
```
cd "3D Printer Remote"
python bots\printer_remote.py detect
python bots\printer_remote.py slice "C:\path\egg_set_plate.3mf" --walls 4 --infill 20 --brim 5
python bots\printer_remote.py printer status
python bots\printer_remote.py printer upload "...\plate_1.gcode" --name egg_set.gcode
python bots\printer_remote.py printer start egg_set.gcode --color white --material PETG   # -> awaiting_owner
#   agent shows the summary, owner says "yes"
python bots\printer_remote.py approve r20261126_141500_ab12 --yes --by owner_chat --quote "yes"
python bots\printer_remote.py filament retract --mm 10 --speed 2 --material PETG                        # -> awaiting_owner
python bots\printer_remote.py watch --interval 120 --stop-when-idle
```

Linux / macOS (set the slicer paths when they aren't the Windows defaults):
```
export CREALITY_PRINT_EXE="$HOME/Applications/CrealityPrint.AppImage"
export CREALITY_DATA_DIR="$HOME/.config/Creality/Creality Print/7.0"
export PRINTER_IP=<PRINTER_IP>   # your printer's LAN address
python3 bots/printer_remote.py detect
python3 bots/printer_remote.py printer status
```

Setup: [SETUP.md](SETUP.md). Agent skill: [skills/printer-remote/SKILL.md](skills/printer-remote/SKILL.md).

## Commands

| command | what | needs owner yes |
|---|---|---|
| `detect` | exe, version, preset dirs, GUI open?, printer IP, camera | no |
| `presets list/show/clone/set` | presets; `set` only on user presets, backed up first | no |
| `slice` | Creality Print CLI slice, gcode + stats + inspector warnings | no |
| `open`, `split-check` | open in the GUI; bed fit, rotation, piece count | no |
| `recommend --part <kind>` | starting settings (kids-toy, car-exterior, car-interior, functional, decor, flat-sign, tall-thin) | no |
| `plates by-color` | split a plain 3MF into one plate per color | no |
| `gcode inspect` | time, grams, cost, temps vs material, pauses, out-of-bed moves | no |
| `queue add/list/skip/move/next` | print queue; `next` uploads and requests a start with the swap note | start does |
| `history list/stats/mark`, `filament ...`, `maint ...`, `library ...`, `timelapse build`, `health` | job tracking | no |
| `printer discover/probe/status/stream/latest/info` | read-only LAN + live data | no |
| `printer files/history/mesh/objects/timelapses/config/ota/materials` | read-only printer queries | no |
| `printer upload` | store gcode on the printer (does not print) | no |
| `printer gcode <line>` | M105/M114/M115/M119/M27/M31/M503/M20 free | anything else does |
| `printer start/pause/resume/cancel/heat/home/move/fan/cooldown`, `printer light` | physical actions | **yes** (light: no; `cooldown --urgent`: no) |
| `filament extrude --mm N` / `retract --mm N` / `load` / `unload` | preheat for the material (`--material PLA`; PETG 240 default; `--purge` = 300 C), wait within 3 C, move filament (M83, then M82), nozzle off | **yes** |
| `approve <id> --yes/--no`, `preflight <id>`, `pending` | owner answer, go/no-go checks, open requests | - |
| `camera snapshot/set-reference/bed-check` | camera | no |
| `watch [--once]` | malfunction watch, job events, alerts | pause asks the owner unless AUTO_PAUSE_ON_ANOMALY=true |

## Approval and preflight

1. A physical command returns `status: awaiting_owner`, a `request_id` and a `summary` (part, time, filament,
   snapshot if a camera is set). An owner_inbox packet `printer_remote - approve <action>_<ts>.json` is written.
2. The agent shows the summary and asks the owner. **Only the owner's own yes or no counts.** The agent passes it on
   with `approve <id> --yes` or `--no` (`--by` names the source, `--quote` keeps the owner's words). It's recorded with
   an ET timestamp.
3. On yes, `preflight` runs: kill switch on, printer online, idle (or printing/paused, as the action needs), no
   printer error, temps sane, the file is on the printer (start), filament on hand (when inventory is used), and the
   camera bed check (flags it; blocks only on a clearly bad score). The action runs only if nothing blocking fails;
   otherwise the status is `preflight_failed` and the failed checks are listed.
4. **Urgent cooldown** is the one exception: `printer cooldown --urgent --reason "..."` turns the heaters off right away
   with no owner yes, for urgent cases only (jam, burning smell, runaway heat or printer error, crash on camera, owner
   says stop, a hot nozzle left after a failure). It's logged to owner_inbox and the safety log. Nothing else can be
   urgent.
5. Requests expire after `APPROVAL_TTL_MIN` (15 by default) and are single use. `PHYSICAL_ACTIONS_ENABLED=false`
   blocks everything, including auto-pause.

## Output contract

Every command prints one JSON object: `status` (`ok`, `error`, `skipped`, `awaiting_owner`, `declined`,
`preflight_failed`, `alert`), `job_id`, `command`, `errors`, `warnings`, `started_at` / `finished_at` (ET) plus its
data. `printer stream --jsonl` prints one JSON line per update, then a `summary` line. Exit code 1 on `error`.

Owner packets: `owner_inbox/printer_remote - <subject>_<YYYYMMDD_HHMMSS>.json`, written as `.tmp` then renamed, with
`id, type, received_at (ET), lane, priority, context{summary, output, status}`.

## Honest limits

See [docs/LIMITS.md](docs/LIMITS.md). In short: the printer protocol isn't verified on a CR-10 SE; the camera checks
are simple image diffs that lighting can fool; the Creality Print CLI crashes on some 7.x builds (issue #574).

## Credits

Built for The Right Mods by Bentayga Empire LLC. Ideas from
[marcmayol/claude-code-creality-print-plugin](https://github.com/marcmayol/claude-code-creality-print-plugin) and
[sairaph/creality-slicer-mcp](https://github.com/sairaph/creality-slicer-mcp) (both MIT). No code was copied; see
[docs/CREDITS.md](docs/CREDITS.md).

MIT license.
