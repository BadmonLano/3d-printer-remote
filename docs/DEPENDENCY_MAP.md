# Dependency map

- `bots/printer_remote.py` - the whole CLI (stdlib; Pillow/numpy optional for camera).
  - reads: Creality Print exe + `%APPDATA%\Creality\Creality Print\7.0\{system,user}`, `deviceInfo.json`, `.env`
  - writes: `bots/state/` (gcode, logs, backups, pending codes, snapshots, printer_latest.json, watch_state.json),
    `owner_inbox/`, user presets (clone / set, with backup)
  - talks to: printer ports 80 + 9999 on the LAN, camera URL, Telegram API (optional)
- `bots/tests/fake_printer.py` - test engine: fake HTTP upload + websocket printer with live pushes.
- `bots/tests/fake_slicer.py` - test engine: fake CrealityPrint.exe.
- `bots/tests/test_printer_remote.py` - offline suite.
