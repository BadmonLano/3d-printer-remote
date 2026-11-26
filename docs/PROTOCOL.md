# Printer protocol used

> Not yet verified on a CR-10 SE. Sources: Creality K1-family firmware as documented by community projects
> (ha_creality_ws, k2-websocket-re, 3d-cli). Fill in what your unit actually answers under "Observed on hardware".

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
| states | 0 idle, 1 printing, 2 complete; 3/4/5 inferred as failed / cancelled / paused |

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
(rolling, `PR_STREAM_LOG_DAYS`, default 7).

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
- **Not tried yet:** upload, start, pause, resume, cancel, heat (as its own command), home, move, fan, light,
  extrude, load, unload.
