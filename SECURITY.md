# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems: anything that could start, heat, move or cancel a
printer without the owner's yes, leak a token, or let an outsider control the printer.

Report it privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**.
Include what you found, how to reproduce it, and what an attacker could do with it.

## How physical actions are approved

- Every physical action (start, pause, resume, cancel, heat, home, move, fan, motion/heat gcode) creates a request
  and stops. Nothing reaches the printer on the first call.
- **Approval is the owner's yes in chat.** The agent records it with `approve <id> --yes --by <source> --quote
  "<owner's words>"`. Agents must only pass along the owner's own answer. They never approve on their own, never treat
  silence, a past yes, an earlier request or a message from anyone else as a yes, and never answer a request they
  opened themselves without asking.
- This is a trust-based gate: the CLI can't tell who typed the yes. It keeps the agent honest and leaves an audit
  trail (request, answer, source, owner's words, ET time, preflight result in `bots/state/approvals/`). It doesn't stop
  a malicious program that already has shell access on the machine.
- On yes, an automatic preflight runs before anything is sent: kill switch, printer online, the right state, no error,
  sane temps, file on the printer, filament on hand, and the camera bed check. Failures stop the action.
- Requests are single use and expire (15 min by default). `PHYSICAL_ACTIONS_ENABLED=false` blocks every physical
  action, including `AUTO_PAUSE_ON_ANOMALY`; the check sits inside the function that talks to the printer.
- Move limits: absolute moves stay inside the bed (`BED_X_MM`, `BED_Y_MM`, `MAX_Z_MM`), relative moves stay at or
  under 50 mm per axis, and nothing moves while a print is running (unless it's paused).

## Keeping your setup safe

- Tokens (Telegram) are read by name from `.env` and never printed. Error messages mask the bot token.
- Stock Creality OS has **no authentication** on ports 80 and 9999, so anyone on your Wi-Fi can talk to the printer.
  Keep it on a trusted network and never port-forward it.
- `owner_inbox/` and `bots/state/` hold snapshots of your room and job history; keep them private.
