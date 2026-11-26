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
10. **Tests:** `python -m unittest discover -s bots/tests -v` (offline: fake slicer, printer, camera and Telegram; no
    extra installs). Optional: `pip install -r requirements-dev.txt` adds pytest, and then `python -m pytest bots/tests` works too.

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

## Farm mode (2.0, optional)

Nothing here is needed for one printer. Without `printers.json` and `policy.json` the remote works like 1.x.

### 1. Fleet
Copy `printers.example.json` to `printers.json` (gitignored) and list your printers. Keep IPs out of it: either name an
env var (`"ip_env": "PRINTER_IP_SE1"` and put `PRINTER_IP_SE1=...` in `.env`) or put `{"se1": "<printer ip>"}` in the
gitignored `printers.local.json`. Or build it from the CLI:
```
python bots/printer_remote.py fleet discover --subnet 192.0.2.0/24      # use your own subnet; read-only
python bots/printer_remote.py fleet add se1 --ip-env PRINTER_IP_SE1 --material PETG --color white --nozzle 0.4 --groups petg
python bots/printer_remote.py fleet set se1 --material PLA --color black   # after a spool swap
python bots/printer_remote.py fleet status
python bots/printer_remote.py --printer se1 printer status
```
Per-printer fields: `name`, `ip_env`, `backend` (`creality_ws` default, or `moonraker` with `moonraker_port` and
`moonraker_api_key_env`), `model`, `bed_mm` (`[x, y, z]`), `nozzle_mm`, `material`, `color`, `groups`, `tags`,
`cameras` (`[{"name": "top", "url_env": "CAMERA_URL_SE1"}]`), `maintenance` (task -> hours, overrides the defaults).

### 2. Farm queue
```
python bots/printer_remote.py farm add part.gcode --material PETG --color white --copies 4 --priority 5 --batch petg-white
python bots/printer_remote.py farm add lid.gcode --depends <job_id>       # waits for the base job
python bots/printer_remote.py farm intake orders.csv                       # columns: order_id,file,material,color,copies,priority,nozzle,due
python bots/printer_remote.py farm plan                                    # dry run
python bots/printer_remote.py farm dispatch                                # uploads + asks the owner per start
python bots/printer_remote.py farm sync                                    # marks done/failed, fails over
```

### 3. Autonomy policy (opt-in)
Copy `policy.example.json` to `policy.json` (gitignored). Levels: `owner_yes` (default), `auto_after_preflight`,
`notify_only`. Check before trusting it: `python bots/printer_remote.py policy check start --printer se1 --profile petg-trusted`.
Kill switch: `policy kill --reason "..."`; lift with `policy unkill` (owner).

### 4. Service and dashboard
```
python bots/printer_remote.py serve                        # http://127.0.0.1:8787  (Ctrl+C or `serve --stop`)
python bots/printer_remote.py serve --watch --auto-dispatch --interval 30
python bots/printer_remote.py token create --role owner --name phone   # shown once
```
On Windows, run it at logon with Task Scheduler (`pythonw bots\printer_remote.py serve`). Health: `GET /api/health`.

### 5. Alerts
`.env`: `ALERT_WEBHOOK_URLS` (comma separated), `ALERT_WEBHOOK_FORMAT` (`json`, `slack`, `discord`),
`ALERT_WEBHOOK_SECRET` (optional signature), `ALERT_EVENTS` (default all: done, failed, anomaly, approval_needed,
filament_low, maintenance_due), `ALERT_DEDUP_MIN`. Telegram uses the existing `TELEGRAM_*` settings.
`python bots/printer_remote.py alerts test`.

### 6. Hardware self-test
`python bots/printer_remote.py selftest hardware` lists what is still unproven on your printer and the safe order to
try it. `--run <step>` makes one normal gated request; the result goes to `docs/PROTOCOL.md`.

### New settings (2.0)
| name | default | what |
|---|---|---|
| `PR_FLEET_FILE` / `PR_FLEET_LOCAL_FILE` | `printers.json` / `printers.local.json` | fleet config |
| `PR_POLICY_FILE` | `policy.json` | autonomy policy |
| `PR_WS_CONNECT_RETRIES`, `PR_WS_CONNECT_TIMEOUT_S`, `PR_WS_BACKOFF_S`, `PR_WS_BACKOFF_CAP_S` | 2, 5, 0.5, 15 | connect retries + backoff |
| `PR_STATUS_RETRIES` | 1 | extra status read attempts (read-only) |
| `PR_STREAM_STALE_S` | 30 | silence that counts as a dropped stream |
| `SERVE_HOST`, `SERVE_PORT`, `SERVE_INTERVAL_S`, `SERVE_LOCAL_TRUST` | 127.0.0.1, 8787, 30, viewer | service |
| `ALERT_WEBHOOK_URLS`, `ALERT_WEBHOOK_FORMAT`, `ALERT_WEBHOOK_SECRET`, `ALERT_EVENTS`, `ALERT_DEDUP_MIN` | -, json, -, all, 15 | alerts |
| `MAINT_BLOCK_DISPATCH` | false | skip printers with overdue maintenance when dispatching |
| `FARM_START_GRACE_S` | 90 | how long a just-started job may still look idle |
| `DETECTOR_ML_URL`, `DETECTOR_ML_MODE`, `DETECTOR_ML_THRESHOLD` | -, post (or obico), 0.6 | ML failure detector hook |
| `ORCASLICER_EXE`, `PRUSASLICER_EXE` | - | optional slicer backends |
| `SELFTEST_VERIFY_S` | 45 | how long a self-test waits to see the result |
