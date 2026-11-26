# Honest limits

**Not yet hardware-verified on a CR-10 SE** for uploads and physical actions. Verified against real hardware:
Creality Print 7.2 detection, presets, settings files built from real presets, split-check on a real 3MF, and (on
2026-11-26, read-only) `printer probe`, `status`, `info`, `files`, `history`, `config`, `mesh` and `objects` on a real
CR-10 SE, plus one owner-approved `filament retract` (heat to 300 C, pull 10 mm, nozzle off), which worked. Upload,
start, pause, resume, cancel, heat, home, move, fan, light, extrude, load, unload and the gcode console have only
run against the fake printer. Details: docs/PROTOCOL.md, "Observed on hardware".

## Printer protocol
- Stock Creality OS LAN flow (HTTP `POST /upload/<file>` on port 80, websocket on 9999) is documented by community
  work on the K1 family. The CR-10 SE runs Creality OS and supports LAN printing from Creality Print, so the same
  flow is expected, **but no public CR-10 SE spec confirms it**. `printer probe` tells you what your unit answers.
- On the real SE the K1 field names matched (see PROTOCOL.md). Websocket field names (`nozzleTemp`, `targetNozzleTemp`, `bedTemp0`, `printProgress`, `printLeftTime`, `layer`,
  `TotalLayer`, `modelFanPct`, `curPosition`, `err`...) come from K1 firmware. Missing fields come back `null`; the
  raw keys are kept in `printer_latest.json` (`raw`) and `printer status` (`raw_keys`) so mappings can be fixed.
- State codes (0 idle, 1 printing, 2 complete, 3 failed, 4 cancelled, 5 paused) are inferred.
- `heat`, `home`, `fan` and `move` send gcode through `gcodeCmd` (M104/M140/G28/M106/G91+G1): untested on the SE.
- `reqElapseVideoList`, `reqMaterials` and `reqOtaInfo` got no specific reply on the SE (DWIN V1.1.0.28); those
  commands warn. The CR-10 SE has no built-in camera (`video: 0`): use `CAMERA_URL` with your own camera.
- The gcode folder on the printer (`PRINTER_GCODE_ROOT`) differs by model; confirm it after the first upload.
- No authentication exists on the printer's LAN ports. Anyone on the network can reach it.
- Moonraker (7125) only exists on rooted units; probe reports it but nothing depends on it.

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
