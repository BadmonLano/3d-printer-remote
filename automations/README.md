# Automations

Prompts to paste into your agent as recurring jobs. None of them can start, pause or cancel a print on their own.
A pause needs the owner's yes in chat plus a passing preflight, unless the owner sets AUTO_PAUSE_ON_ANOMALY=true
(which `PHYSICAL_ACTIONS_ENABLED=false` still blocks).

- `printer_watch.automation.md`: every 2 minutes while a print runs.
