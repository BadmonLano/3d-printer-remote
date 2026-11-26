# Honest limits

Verified on hardware means it ran on **one** real CR-10 SE (Creality OS, DWIN V1.1.0.28, Wi-Fi) with the owner's OK.
Fake-tested means it only ran against the fake printers in `bots/tests/`. Nothing has run on a second printer, a
K1, a rooted printer, or a real farm.

| area | status | when (ET) / how |
|---|---|---|
| Creality Print 7.2 detect, presets, settings files, split-check, CLI slice | hardware | 2026-11-26 |
| `printer probe/status/info/files/history/config/mesh/objects`, `printer stream` | hardware | 2026-11-26 (read-only) |
| `filament retract` 10 mm / `unload` 80 mm / `extrude` 30 mm (300 C purge) | hardware | 13:29 / 14:00 / 14:09 |
| `printer start` (owner yes, preflight, opGcodeFile) | hardware | 14:21 |
| `printer light off/on` (mid-print) | hardware | 15:29 |
| `printer fan --pct 70` then back to 50 (mid-print; slicer gcode did not override within 20 s) | hardware | 15:30 |
| `printer heat --nozzle 235` then 240 (mid-print, 5 C change) | hardware | 15:30-15:31 |
| `printer pause` (77 s) then `resume` (mid-print) | hardware | 15:31-15:34 |
| upload, cancel, home, move, load, idle `cooldown`, urgent cooldown, gcode console | **fake only** | run `selftest hardware` |
| reconnect/backoff, stream drop recovery, continuity events, power-loss awareness | **fake only** (chaos suite) | the SE does report `repoPlrStatus` (0 so far) |
| fleet, discovery, farm queue/routing/failover, policies, kill switch, audit | **fake only** | multi-instance fake printers |
| `serve`, dashboard, REST API, tokens/roles, alerts/webhooks | **fake only** | loopback tests |
| maintenance per printer, spools, analytics, order intake | **fake only** | |
| Orca/Prusa slicer backends, ML detector hook, Moonraker backend | **stubs**, fake only | no real Orca/Prusa/Obico/Moonraker was run |

What the mid-print tests showed (2026-11-26, white PETG egg print, owner away, owner OK at 3:27 PM ET):
- Pause on the SE is not just a stop: the firmware parks the head at X0 Y222, lifts Z about 46 mm, drops the nozzle to
  140 C and keeps the bed at 80 C. On resume it reheats 140 -> 240 C (about 75 s) before moving back, so a "1 minute"
  pause costs about 3 minutes. Keep pauses short on PETG so the part doesn't cool off the bed.
- During that reheat the printer reports `state 0` with `deviceState 1`. 1.x labelled that "idle"; 2.0 labels it
  `busy` (`idle` stays false, so the fleet never treats it as free).
- `M106` and `M104` sent over `gcodeCmd` act at once mid-print; the target shows in the next status push.
- `lightSw` toggles the light and `light_on` follows within a few seconds.

## Printer protocol
- Stock Creality OS LAN flow (HTTP `POST /upload/<file>` on port 80, websocket on 9999) is documented by community
  work on the K1 family. The CR-10 SE runs Creality OS and supports LAN printing from Creality Print, so the same
  flow is expected, **but no public CR-10 SE spec confirms it**. `printer probe` tells you what your unit answers.
- On the real SE the K1 field names matched (see PROTOCOL.md). Websocket field names (`nozzleTemp`, `targetNozzleTemp`, `bedTemp0`, `printProgress`, `printLeftTime`, `layer`,
  `TotalLayer`, `modelFanPct`, `curPosition`, `err`...) come from K1 firmware. Missing fields come back `null`; the
  raw keys are kept in `printer_latest.json` (`raw`) and `printer status` (`raw_keys`) so mappings can be fixed.
- State codes (0 idle, 1 printing, 2 complete, 3 failed, 4 cancelled, 5 paused) are inferred.
- `heat` (M104) and `fan` (M106) through `gcodeCmd` work on the SE (2026-11-26). `home` (G28), `move` (G91+G1) and
  bed heat (M140) on its own are still untested there.
- `reqElapseVideoList`, `reqMaterials` and `reqOtaInfo` got no specific reply on the SE (DWIN V1.1.0.28); those
  commands warn. The CR-10 SE has no built-in camera (`video: 0`): use `CAMERA_URL` with your own camera.
- The gcode folder on the printer (`PRINTER_GCODE_ROOT`) differs by model; confirm it after the first upload.
- No authentication exists on the printer's LAN ports. Anyone on the network can reach it.
- Moonraker (7125) only exists on rooted units. 2.0 adds a Moonraker backend (`"backend": "moonraker"` in
  printers.json) for status, upload, start, pause, resume, cancel and gcode-based heat/fan/home/move; it is tested
  only against a fake Moonraker. Filament moves are not implemented for it.

## Slicer
- Creality Print is a GUI app: the CLI writes little to the console, shares config with the window (close it first),
  and crashes on some 7.x builds (issue #574, exit 3221225477). Some 7.2 builds want underscore flags, others dashes;
  `slice` tries underscore then falls back to dash on "Invalid option".
- `--export_3mf` is not used (reported to crash on 7.2.2).
- Gcode stats are parsed from comments; if a build changes the wording they come back missing, not wrong.

## Camera and malfunction watch
- The bed-clear check is a grayscale image difference against one empty-bed photo. Lighting, shadows, a moved
  camera, the bed at a different Y position, or a bed sheet swap all change the score. It **flags**, never approves.
- Watch heuristics: big frame change between checks (spaghetti, knocked-off part - or lights turned on), no visible
  change / no progress for `WATCH_STALL_MINUTES` while printing, temp runaway above target, temp drop below target
  after warm-up, heating without a target while idle, printer error codes. It will miss slow failures (gradual
  spaghetti, warping) and can false-alarm on lighting changes. It does not detect fire or smoke.
- With `--interval 120`, a problem can run up to 2 minutes before it's seen. Watch must keep running (agent job or
  loop); if the PC sleeps, nothing is watched.
- Auto-pause is off by default. A pause still needs the owner's yes and a passing preflight.

## Approval and preflight
- Approval is trust-based: the CLI records the yes the agent passes on, with the source and the owner's words. It
  can't prove who typed it. Agents must only pass on the owner's own answer.
- Preflight reads printer state at that moment; things can change in the seconds before the command lands.
- `file_on_printer` is only checked when the printer lists its files; otherwise it's flagged, not blocking.
- Filament checks only work if you keep the inventory (`filament add/use`).
- The camera bed check blocks only at `BEDCHECK_BLOCK_FACTOR` x threshold; smaller differences are flagged.

## Job features
- Grams, time and cost come from slicer comments in the gcode. ETA comes from the printer's `printLeftTime`.
- Maintenance hours count only prints started through this tool.
- Timelapse needs ffmpeg for mp4; without it a GIF is built (needs Pillow).
- Telegram is optional and only used when both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.

## Filament preheat and urgent cooldown
- Filament preheat follows the material spec: PLA 210, PETG 240, ABS 245, ASA 250, TPU 225 C (middle of the usual
  published ranges), PETG when no material is given. `FILAMENT_PREHEAT_C` in a local `.env` overrides it for one
  owner. `--purge` (or `--temp 300`) heats to 300 C to melt hardened filament from the tip. 300 C is close to the stock
  CR-10 SE hotend rating, so filament moves turn the nozzle off afterwards unless `--hold`.
- One nozzle ceiling for everything: 300 C (`printer heat`, filament moves, purge). `FILAMENT_MAX_C` can lower it but
  not raise it. The printer itself reports 320 C as its max; we stay 20 C under it. `printer heat` above 260 C (the top of
  normal printing ranges) is allowed but warns that it's purge territory.
- Urgent cooldown only turns heaters off. It can't undo a crash or clear a jam, and if the printer is unreachable it
  can only tell you to switch the printer off at the power switch.
- Urgent cooldown during a print ends that print (the heaters go off).

## Farm mode (2.0)
- **No real farm has run this.** Routing, failover, the service and the dashboard were exercised with 2-3 fake printers
  on one machine. Real farms will hit things the fakes don't model: Wi-Fi roaming, firmware differences, printers that
  report `complete` late, people pulling parts off beds. Run it supervised first.
- **Bed clearing is still human.** After a job finishes, a farm printer is "available" again as soon as it reports
  idle. Nothing confirms the part was removed unless a camera bed check passes, and that check is advisory. With
  `auto_after_preflight`, keep a camera on every printer and the bed check on, or keep starts at `owner_yes`.
- **Auto-start is only as good as preflight.** `auto_after_preflight` runs a start with nobody watching. Preflight can't
  see a stuck nozzle, a tangled spool or a part left on the bed without a camera. It's rate limited and audited, and
  heat/move/home/fan/gcode/filament moves/cancel never run automatically.
- **Loaded material is what you tell it.** Routing trusts `printers.json` (`fleet set --material --color`) or a spool
  moved onto the printer (`ops spool --move`). The SE's filament sensor is off on this unit, so nothing checks.
- **Job completion** is read from printer state (`complete`, or idle after >= 99 %). A job that ends idle below 99 %
  counts as failed and fails over to another printer. Finishing while the service is down is caught on the next poll.
- **Power loss:** the remote reports `repoPlrStatus` and never auto-resumes; resume vs cancel is decided on the printer
  screen.
- **Service:** one process, JSON files for state (atomic writes, no database). The API serializes its own writes, but
  running CLI commands that change the same queue at the same moment as the service can race. No HA, no clustering,
  no TLS (put it behind a reverse proxy with TLS if it ever leaves localhost).
- **Discovery** only finds printers that answer on port 9999 in the subnet you scan (max /22). It never changes them.
- **Alerts:** webhooks are fire-and-retry (2 retries); a down endpoint drops the alert after that (it's still in
  `owner_inbox/` and `alerts list`).
- **Detectors:** the ML hook calls an external server you run (for example Obico's ml_api). It's not bundled, and the
  Obico request shape was written from its docs, not tested against it.
- **Slicers:** OrcaSlicer and PrusaSlicer are CLI stubs (argv only). Check their gcode before printing.
