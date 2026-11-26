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
  action, including `AUTO_PAUSE_ON_ANOMALY` (urgent cooldown excepted); the check sits inside the function that talks to the printer.
- Urgent cooldown (`printer cooldown --urgent --reason`) is the only action without the owner's yes. It can only set
  heaters to 0 (and optionally the part fan to max), needs a reason, and is logged with the ET time. It ignores the kill
  switch on purpose, since removing heat is the safe direction. If the printer can't be reached it says so and tells
  the owner to switch the printer off at the power switch.
- Nozzle ceiling: 300 C for every command (heat, filament moves, purge), 20 C under the printer's reported 320 C max.
- Filament moves: preheat per material (PETG 240 by default), limited mm per command and speed, refused mid-print,
  and the nozzle is always set back to 0 on success (unless `--hold`) or on any failure.
- Move limits: absolute moves stay inside the bed (`BED_X_MM`, `BED_Y_MM`, `MAX_Z_MM`), relative moves stay at or
  under 50 mm per axis, and nothing moves while a print is running (unless it's paused).

## Keeping your setup safe

- Tokens (Telegram) are read by name from `.env` and never printed. Error messages mask the bot token.
- Stock Creality OS has **no authentication** on ports 80 and 9999, so anyone on your Wi-Fi can talk to the printer.
  Keep it on a trusted network and never port-forward it.
- `owner_inbox/` and `bots/state/` hold snapshots of your room and job history; keep them private.

## Farm mode (2.0)

### Autonomy
- Default is unchanged: every physical action waits for the owner's yes. Autonomy only exists if you create a
  `policy.json`. `auto_after_preflight` can only cover start, pause, resume and cooldown, and only the actions listed in
  `auto_actions` in policy.json (default: start only). Heat, home, move, fan, gcode, filament moves, cancel and `selftest hardware` steps
  always need the owner. A job profile overrides the printer's level, a printer overrides its groups (the safest group wins), a profile only
  applies when its `materials`/`printers` limits match, and a printer or group set to `notify_only` can't be loosened by
  a profile.
- Auto runs still pass the full preflight, are rate limited (`rate_limits` in policy.json, default 6/hour farm-wide
  and 3/hour per printer) and are written to the audit log with `by: policy:auto_after_preflight`.
- **Risk:** an auto start with nobody in the room relies on preflight and the camera bed check. Without a camera,
  nothing can tell that the last part is still on the bed. Use auto-start only with a camera on every printer, or only
  for printers someone clears every time.
- Kill switch: `policy kill [--printer X] --reason ...` blocks every physical action and auto-dispatch at once (CLI,
  API `POST /api/kill`, dashboard button). Lifting it (`policy unkill`) needs the owner role. Urgent cooldown still
  works while killed.

### Service, tokens and roles
- `serve` binds to `127.0.0.1` by default. Local requests with no token get the `SERVE_LOCAL_TRUST` role (default
  `viewer`: read only). Binding to another address refuses to start unless at least one owner or operator token exists,
  and every non-local request then needs a token.
- Tokens: `token create --role owner|operator|viewer --name ...` prints the token once; only a PBKDF2 hash is stored in
  `bots/state/tokens.json`. Revoke with `token revoke`. Send as `Authorization: Bearer <token>` or `X-API-Token`.
- Roles: **viewer** reads status, queue, history, analytics and camera; **operator** adds farm jobs, dispatches (which
  still asks the owner unless policy says otherwise), kills, and makes gated requests; **owner** also answers approvals
  through the API, lifts the kill switch, changes policy and manages tokens. Approving through the API records the
  token's name as the source; it's still the owner's yes and must be the owner's own.
- Every API call that changes something and every CLI action is in `bots/state/audit.jsonl` with the actor.
- There is no TLS. Don't expose `serve` to the internet. For access away from home, use a VPN (WireGuard, Tailscale)
  to the PC, or a reverse proxy with TLS and the tokens above.

### Network isolation (recommended for a farm)
- Put printers on their own VLAN or SSID (an IoT network) with no internet access unless you need firmware updates.
- Allow only the PC running `serve` to reach printer ports 80 and 9999 (firewall rule on the router or the VLAN).
- Never port-forward 80, 9999 or 7125. Treat anyone on the printer network as able to start, heat or cancel prints.
- `fleet discover` only connects to port 9999 and reads status; it never changes a printer. Keep scans to your own
  subnet.

### Webhooks and alerts
- `ALERT_WEBHOOK_URLS` are secrets (they let anyone post to your channel). Keep them in `.env`. Set
  `ALERT_WEBHOOK_SECRET` to have each JSON alert signed (`X-PR-Signature: sha256=<hmac>`) so a receiver can check it.
- Alerts never contain printer IPs or tokens.
