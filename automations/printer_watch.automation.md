# Printer watch lane (every 2 minutes)

From the project root, run `python bots/printer_remote.py printer latest`. If `printer.printing` is false and no
print was started in the last 10 minutes, stop here.

Otherwise run `python bots/printer_remote.py watch --once` and read `last`:
- `anomalies` empty: nothing to say. Pass along `events` (first layer, ETA, done + "clear the bed") in one short line.
- `status: alert`: tell the owner in one short message what it saw (`alerted[].detail`), the ET time and the snapshot
  path. If `action.pause_requested` is true, ask "Pause it? yes or no". Run
  `approve <action.request_id> --yes --by owner_chat --quote "<their words>"` only after the owner says yes.
- `warnings` that the printer or camera was unreachable 3 times in a row: tell the owner once.
