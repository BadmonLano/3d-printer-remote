# Credits

No source code was copied. These projects (both MIT) informed the design:

- **marcmayol/claude-code-creality-print-plugin** (Copyright (c) 2026 Marc Mayol Orell, MIT) -
  https://github.com/marcmayol/claude-code-creality-print-plugin
  Ideas: finding Creality Print, checking success by exit code + `plate_*.gcode` (not log lines), 7.3 `--cli` /
  `--need-gcode-file`, reading the printer IP from Creality Print's `deviceInfo.json`, the port 9999 websocket
  messages (`opGcodeFile`, `pause`, `stop`, heart_beat -> "ok").
- **sairaph/creality-slicer-mcp** (Copyright (c) 2026 Sairaph, MIT) - https://github.com/sairaph/creality-slicer-mcp
  Ideas: Windows-first Creality Print CLI driving, preset handling, exit-code hints.
- OrcaSlicer issue #2103 and community K1 notes for the `POST /upload/<file>` + ws 9999 LAN flow.
- Creality Print issue #574 for the known CLI crash (0xC0000005).
