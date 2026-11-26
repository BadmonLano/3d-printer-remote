# Timeline

Version history, newest first. Details: [CHANGELOG.md](CHANGELOG.md).

## v2.0.0: farm control plane
- Fleet, shared farm queue with routing and failover, opt-in autonomy policies with kill switch and audit, `serve`
  daemon with dashboard and REST API, tokens and roles, webhook alerts, maintenance/spools/analytics, pluggable
  slicers/detectors/playbooks, Moonraker stub, chaos suite and `selftest hardware`. Pause, resume, fan, heat and light
  verified mid-print on the real CR-10 SE; farm features fake-tested only.

## v1.2.2: slice origin fix and real-print notes
- Creality Print 7.2 settings-file origin fix, print-quality skill checklist, unittest docs,
  and hardware notes for retract, unload, extrude and an owner-approved start.

## v1.2.1: urgent cooldown
- Heaters-off without asking, for urgent cases only; real printer address out of tracked files.

## v1.2.0: filament control
- Owner-approved extrude, retract, load and unload with a 300 C preheat, plus cooldown.

## v1.1.0: owner approval + jobs
- Owner yes/no in chat plus automatic preflight for every physical action; gated move/home/fan; print queue with
  filament swaps, history, cost, inventory, maintenance, gcode inspector, job notices, health report. Not yet
  hardware-verified on a CR-10 SE.

## v1.0.0: first release
- Creality Print CLI slicing and presets, bed-fit check, live printer data, LAN upload, owner-gated physical
  actions, camera bed-clear check and malfunction watch, agent skill.
