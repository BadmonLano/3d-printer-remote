# Printer protocol used

> Sources: Creality K1-family firmware as documented by community projects (ha_creality_ws, k2-websocket-re, 3d-cli),
> then checked on one real CR-10 SE (see "Observed on hardware" and the self-test log at the end).

| what | how |
|---|---|
| connect | websocket `ws://<ip>:9999`, subprotocol `wsslicer`; reply `ok` to `{"ModeCode":"heart_beat"}` |
| live data | `{"method":"get","params":{"ReqPrinterPara":1}}`; the printer then pushes JSON diffs |
| printer info | `GET http://<ip>/info` (model, version) when present |
| upload | `POST http://<ip>:80/upload/<name>` multipart, field `file` |
| files / history | get `reqGcodeFile` (reply `retGcodeFileInfo2`), `reqHistory` (reply `historyList`) |
| other reads | `reqProbedMatrix` (mesh), `reqPrintObjects`, `reqElapseVideoList` (timelapses), `reqPrinterCfg`, `reqOtaInfo`, `reqMaterials` |
| start | `{"method":"set","params":{"opGcodeFile":"printprt:<root>/<file>"}}` |
| pause / resume / cancel | `{"pause":1}` / `{"pause":0}` / `{"stop":1}` |
| heat | `{"gcodeCmd":"M104 S240"}`, `{"gcodeCmd":"M140 S75"}` |
| home / fan / move | `{"gcodeCmd":"G28"}`, `{"gcodeCmd":"M106 S128"}`, `G91` + `G1 Z10 F600` + `G90` |
| light | `{"lightSw":1}` / `0` |
| states | 0 idle, 1 printing, 2 complete; 3/4/5 inferred as failed / cancelled / paused; 5 = paused confirmed on the SE; state 0 + deviceState 1 = busy (resume reheat) |
| power loss | `powerLoss`, `repoPlrStatus` (non-zero = a recovery record is waiting; 0 on every read so far) |

## Live data JSON (normalized)

```
{"received_at":"2026-11-26 12:30:05 ET","state":"printing","state_code":1,"printing":true,"paused":false,
 "file":"egg.gcode","progress_pct":42,"layer":37,"total_layers":120,"elapsed_s":1800,"remaining_s":2500,
 "nozzle":{"actual":239.6,"target":240,"max":null},"bed":{"actual":74.8,"target":75,"max":null},
 "chamber":{"actual":null},"fans_pct":{"part":50,"aux":0,"case":0},"position":{"X":101.2,"Y":88.0,"Z":7.4},
 "speed_pct":null,"flow_pct":null,"error":{"code":0,"key":0,"raw":{...}},"has_error":false,
 "model":"F003","hostname":"...","firmware":null}
```

Files: `bots/state/printer_latest.json` (latest + `raw`), `bots/state/logs/printer_stream_YYYYMMDD.jsonl`
(rolling, `PR_STREAM_LOG_DAYS`, default 7). Fleet printers (2.0) keep theirs under `bots/state/printers/<name>/`.

`printer stream` (2.0) survives drops: it reconnects with exponential backoff (`PR_WS_BACKOFF_S`, cap
`PR_WS_BACKOFF_CAP_S`), treats `PR_STREAM_STALE_S` seconds of silence as a drop, re-requests `ReqPrinterPara` after
each reconnect, and returns `reconnects`, `drops` and `continuity_events` (`job_lost`, `job_finished_while_away`,
`power_loss_resume_pending`, `printer_error`, `job_changed`).

## Moonraker (rooted printers, 2.0 stub, fake-tested only)

| what | how |
|---|---|
| status | `GET :7125/printer/objects/query?print_stats&extruder&heater_bed&virtual_sdcard&fan&webhooks` |
| upload | `POST /server/files/upload` multipart `file` |
| start / pause / resume / cancel | `POST /printer/print/start?filename=` / `pause` / `resume` / `cancel` |
| heat / fan / home / move / gcode | `POST /printer/gcode/script?script=` |
| auth | `X-Api-Key` from the env var named in `moonraker_api_key_env` |

## Observed on hardware (real CR-10 SE, 2026-11-26, read-only)

Unit: model `F003`, firmware string `DWIN hw ver:F003;DWIN sw ver:V1.1.0.28`, Wi-Fi.
- Ports: **80 open** (`Server: httpd/1.25.7.29`, `GET /info` -> `{"mac":..., "model":"F003"}`) and **9999 open**.
  7125 / 4408 / 4409 / 8080 closed (no Moonraker / Fluidd on stock firmware).
- `ReqPrinterPara` works over `wsslicer`. The K1 field names match: `state`, `deviceState`, `nozzleTemp`,
  `targetNozzleTemp`, `maxNozzleTemp` (320), `bedTemp0`, `targetBedTemp0`, `maxBedTemp` (130), `printProgress`,
  `dProgress`, `layer`, `TotalLayer`, `printJobTime`, `printLeftTime`, `printFileName`, `modelFanPct`,
  `auxiliaryFanPct`, `caseFanPct`, `curPosition` (`"X:0.00 Y:0.00 Z:0.00"`), `curFeedratePct`, `curFlowratePct`,
  `err` (`{"errcode":0,"key":0}`), `boxTemp`, `hostname`, `model`, `modelVersion`.
- Extra keys on the SE: `bedTemp1/2`, `targetBedTemp1/2`, `lightSw`, `tfCard`, `materialDetect`, `materialStatus`,
  `powerLoss`, `repoPlrStatus`, `autohome`, `autoLevelResult`, `pressureAdvance`, `smoothTime`, `velocityLimits`,
  `accelerationLimits`, `accelToDecelLimits`, `cornerVelocityLimits`, `realTimeSpeed`, `realTimeFlow`,
  `usedMaterialLength`, `printId`, `printStartTime`, `upgradeStatus`, `video`, `video1` (0: no camera),
  `videoElapse*`, `ai*`, `enableSelfTest`, `withSelfTest`, `nozzleMoveSnapshot`, `*AutoPid`, `fan`, `fanAuxiliary`,
  `fanCase`, `connect`.
- Numbers often come as strings (`"22.730000"`); the normalizer converts them.
- Idle after a cancelled job reported `state: 4` with `deviceState: 0`.
- `reqGcodeFile` answers **`retGcodeFileInfo`** (not `...2`): `{"totalNum": 15, "fileInfo": "dir:name:size:?:ctime:?:
  thumb;..."}`. The gcode root is `/usr/data/printer_data/gcodes` (same as K1). Parsed since 1.1.0.
- `reqHistory` works: `totalJob`, `totalUsageTime`, `totalUsageMaterial`, `historyList[]` with `id, filename, size,
  ctime, starttime, startway, usagetime, usagematerial, printfinish, thumbnail, filemd5`.
- `reqPrinterCfg` works: `printerCfgInfo` (stepper_x max 243 / min -10.5, stepper_y 225 / -5.5, stepper_z 270 / -5,
  extruder, heater_bed, bed_mesh, printer).
- `reqProbedMatrix` works: `probedMatrix {num: 49, val: [{x, y, z}...]}` (7x7 mesh).
- `reqPrintObjects` works: `current_object`, `excluded_objects`, `objects`.
- `reqElapseVideoList`, `reqMaterials`, `reqOtaInfo`: no specific reply (status push only).
- 13:13 ET re-check: same IP, status + 15 s `printer stream` gave 5 updates (~one every 3-5 s while idle), nozzle
  23.5 C, bed 22.4 C, both targets 0, state 4 / deviceState 0, no error.
- Normalized since 1.1.0: `idle`, `firmware_version`, `light_on` (lightSw), `storage_present` (tfCard),
  `filament_sensor` (materialDetect/materialStatus), `used_material_mm`, `realtime_speed_mm_s`, `realtime_flow_mm3_s`,
  `power_loss_recovery` (repoPlrStatus), `print_id`, `camera_on_printer` (video/video1).
- **First physical action, approved by the owner (2026-11-26 13:29 ET): `filament retract --mm 10 --speed 2`.**
  `gcodeCmd` works on the SE: `M104 S300` set the target at once (`targetNozzleTemp` 300), the nozzle went from
  24.1 C to 296+ C in about 80 s (about 5 C/s at its fastest), reached 300 C, `M83` / `G1 E-10 F120` / `M82` were
  accepted, and `M104 S0` set the target back to 0. 97 s total. 30 s later the nozzle read 222 C with target 0, idle,
  no error. Pushes arrive every ~2 s while heating.
- **Owner-approved `filament unload` (2026-11-26, ~2:00 PM ET, 300 C purge):** heated to 300 C in about 77 s, pushed
  5 mm, pulled 80 mm (chunked), `M104 S0`. Nozzle read 226 C with target 0 shortly after, no error. The owner
  confirmed the filament came out.
- **Owner-approved `filament extrude --mm 30 --temp 300` (2026-11-26 2:09 PM ET):** preflight passed (state read
  "cancelled", left over from an earlier job, which counts as idle; nozzle 37.5 C, bed 23.5 C, err 0). `M104 S300` at
  1.5 s, 300 C reached at 76.4 s (37 -> 150 C by ~30 s, 260 C by ~61 s), `M83` / `G1 E30 F180` / `M82` at 81.4 s, the
  nozzle held 299-302 C during the push, `M104 S0` at 93.4 s, 96.4 s total. 10 s later: nozzle 276 C, target 0, bed
  23.6 C, no error. Filament sensor is off, so flow out of the nozzle is confirmed by eye only.
- **Owner-approved `printer start` (2026-11-26):** preflight passed (no camera, so no bed check). Creality OS ran
  its own home/probe first (nozzle 150 C then 200 C wipe, then 140 C, heater off while probing with the bed at 60 C),
  then the slicer's heat commands, with first layer about 8 minutes in. The printer's remaining-time field was wrong
  at layer 1.
- **Mid-print tests, owner OK (2026-11-26, 3:27 PM ET: "you can pause it and play with stuff to make sure it works.
  This is the test print afterall"), white PETG egg print at ~20 %, layer 18, nozzle 240 / bed 80, owner away:**
  - `printer light off` / `on` (15:29): `{"lightSw":0}` -> `light_on` false in the next read (~5 s), `{"lightSw":1}` ->
    true. Not gated (by design).
  - `printer fan --pct 70` (15:30, approved through `approve --by owner_chat --quote ...`): preflight passed, `M106 S179`
    -> `modelFanPct` 70 at once and still 70 after 20 s (the slicer's own fan commands didn't override it in that
    window). `printer fan --pct 50` -> `M106 S128` -> 50 (restored).
  - `printer heat --nozzle 235` (15:30): `M104 S235` -> target 235 at once, actual 234.2 after 8 s and 235.0 after
    ~30 s, no error, print kept going. `printer heat --nozzle 240` (15:31) -> 240.4 after ~30 s (restored).
  - `printer pause` (15:31:56): `{"pause":1}`, reply `state 5`. Within 12 s: head parked at X0 Y222, Z 3.7 -> 50.05,
    nozzle target 240 -> **140** (firmware does this), bed held 80. Held 77 s.
  - `printer resume` (15:33:13): `{"pause":0}`, reply `state 0`. The printer reported `state 0` / `deviceState 1` while
    it reheated 140 -> 240 C (164 C at +12 s, 219 C at +48 s, 239 C at +90 s), then moved back and printed
    (`state 1`, layer 19, 15:34:59). Pause-to-printing total about 3 min. 2.0 labels that reheat state `busy`.
  - Status reads during all of it: `err 0`, `repoPlrStatus 0`.
- **Not tried yet:** upload, cancel, home, move, load, idle cooldown, urgent cooldown, bed heat on its own. Use
  `selftest hardware` (results land in the log below).

## Hardware verification log (`selftest hardware`)

<!-- SELFTEST:BEGIN -->

| step | result | when (ET) | note |
|---|---|---|---|
| retract | pass | 2026-11-26 13:29 | filament retract 10 mm at 300 C, owner approved |
| unload | pass | 2026-11-26 14:00 | unload 80 mm at 300 C; owner saw filament come out |
| extrude | pass | 2026-11-26 14:09 | extrude 30 mm at 300 C |
| start | pass | 2026-11-26 14:21 | started a white PETG test print (opGcodeFile) |
| light | pass | 2026-11-26 15:29 | lightSw 0 then 1 mid-print; light_on followed within 5 s |
| fan | pass | 2026-11-26 15:30 | M106 S179 -> part fan 70% (held 20 s, slicer did not override), M106 S128 -> 50% |
| heat | pass | 2026-11-26 15:30 | mid-print M104 S235: target 235 at once, actual 235 in ~25 s; M104 S240 restored, 240 in ~30 s |
| pause | pass | 2026-11-26 15:31 | {pause:1} -> state 5 paused; firmware parks head X0 Y222, lifts Z ~46 mm, drops nozzle to 140 C, keeps bed 80 C |
| resume | pass | 2026-11-26 15:33 | {pause:0} after 77 s paused; reheats 140->240 C (~75 s, state 0 + deviceState 1), back to printing at 15:34:59 |

<!-- SELFTEST:END -->
