# Setup

> Not yet hardware-verified on a CR-10 SE: run steps 4-5 before you rely on any printer command.

1. **Python 3.12+.** The core needs nothing else. For camera features: `pip install -r requirements.txt` (Pillow,
   numpy). Use `python` on Windows and `python3` on Linux/macOS.
2. **Creality Print 7.2.** On Windows it's found at `C:\Program Files\Creality\Creality Print 7.2\`, with presets under
   `%APPDATA%\Creality\Creality Print\7.0\`. On Linux/macOS (or any non-default install), set the paths:
   ```
   export CREALITY_PRINT_EXE="$HOME/Applications/CrealityPrint.AppImage"   # or /opt/CrealityPrint/CrealityPrint
   export CREALITY_DATA_DIR="$HOME/.config/Creality/Creality Print/7.0"
   python3 bots/printer_remote.py detect
   ```
   On Windows, put the same names in `.env` only if `detect` can't find them.
3. **Copy `.env.example` to `.env`** (gitignored). Set `PRINTER_IP` to the printer's LAN address (from the printer
   screen under network, or the address Creality Print saved; `printer discover` shows what it found). Reserve that IP
   in your router.
4. **Check the printer link (read-only):** `python bots/printer_remote.py printer probe`. You want port 80 (upload)
   and 9999 (live data + control) open. If every port times out, probe returns `error`: check the printer is on, awake
   and on the same network. Then run `printer status`.
5. **Gcode folder on the printer.** The default is `PRINTER_GCODE_ROOT=/usr/data/printer_data/gcodes` (K1 family).
   After the first `printer upload`, check the file shows on the printer screen and in `printer files`.
6. **Close the Creality Print window before CLI slicing.** They share config; `slice` refuses while it's open
   (`--close-gui` closes it, `--allow-gui` risks it).
7. **Camera (optional).** Set `CAMERA_URL` to a JPEG snapshot URL or an MJPEG stream. With the bed empty, run
   `camera set-reference`. Tune `BED_CLEAR_DIFF_THRESHOLD`.
8. **Telegram (optional).** Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for alerts and done photos.
   `APPROVAL_TELEGRAM=true` also sends approval requests there.
9. **Agent.** Point your agent at `skills/printer-remote/SKILL.md`. Paste `automations/printer_watch.automation.md`
   as a recurring job if you want print watching.
10. **Tests:** `python -m unittest discover -s bots/tests -v` (offline: fake slicer, printer, camera and Telegram).

| env | needed | what |
|---|---|---|
| PRINTER_IP | for LAN | printer address (falls back to Creality Print's saved device) |
| PRINTER_GCODE_ROOT | for start | gcode folder on the printer |
| CREALITY_PRINT_EXE / CREALITY_DATA_DIR | Linux/macOS, or if detect fails | slicer binary and preset folder |
| CAMERA_URL | optional | snapshot or MJPEG URL |
| TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID | optional | alerts and photos |
| APPROVAL_TTL_MIN | optional | minutes an approval request stays open (15) |
| PHYSICAL_ACTIONS_ENABLED | optional | `false` blocks every physical action, auto-pause included |
| AUTO_PAUSE_ON_ANOMALY | optional | `false` by default; `true` lets watch pause without asking |
| AUTO_COOLDOWN_ON_ANOMALY | optional | `false` by default; `true` lets watch turn the heaters off on runaway heat or errors |
| FILAMENT_MATERIAL | optional | default material for filament moves (PETG) |
| FILAMENT_PREHEAT_C | optional | personal preheat override when no `--material` is given (cap 300) |
| BED_X_MM / BED_Y_MM / MAX_Z_MM | optional | move limits (220 / 220 / 250) |
| FILAMENT_COST_PER_KG, FILAMENT_LOW_G | optional | cost math and low-spool warning |
| LIBRARY_ROOTS | optional | folders for `library scan` (separated by `;`) |
