# Future updates

## Needs hardware (do these first)
- Finish `selftest hardware` on the CR-10 SE: upload, cancel, home, move, load, idle cooldown, urgent cooldown
  (see LIMITS.md for the list and when each is safe to run).
- Soak test: `serve --interval 30` for days through Wi-Fi drops and a planned power cut, then compare the audit log and
  continuity events with what really happened.
- A second printer (same model, then a different firmware) to prove fleet routing and failover for real.
- Confirm what `repoPlrStatus` reports after a real power cut mid-print.
- A camera per printer, then measure the bed-clear check's false-positive / false-negative rate before trusting
  `auto_after_preflight` overnight.

## Software
- Containerized `serve` (Dockerfile + compose) and Windows service / systemd unit files.
- SQLite instead of JSON files once the queue or history grows past a few thousand rows; centralized logging.
- TLS + reverse-proxy recipe for remote dashboard access (today: localhost or LAN with tokens).
- Real OrcaSlicer / PrusaSlicer runs and profile mapping; plate-level batching (several copies on one bed).
- Obico ml_api tested end to end; per-region image diff that ignores the gantry.
- Filament moves on the Moonraker backend; Moonraker tested on a rooted printer.
- Thumbnail injection via bundled `stl-thumb.exe` so the printer screen shows a preview.
- Spool weight from a smart scale; filament runout handling from the printer's sensor when it's enabled.
