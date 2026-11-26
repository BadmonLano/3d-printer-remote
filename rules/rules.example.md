# Owner rules (example)

Copy to `rules/rules.md` (gitignored) and edit. The agent reads these before printer work.

- Default material: PETG on the CR-10 SE (nozzle 240 C, first layer 235 C, bed 75 C).
- Default process: 0.20mm Standard, 4 walls, gyroid infill, brim 5 mm on tall parts, no supports unless asked.
- Never start a print when nobody is home.
- Ask before slicing anything bigger than 215 mm; run split-check first.
- Watch every print longer than 2 hours (`watch --interval 120 --stop-when-idle`).
