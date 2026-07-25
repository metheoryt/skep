## Start here

Read [`ARCHITECTURE.md`](ARCHITECTURE.md) before touching code. It is the only
steady-state description of skep: the three processes, the inbound/outbound
message paths, the transport seam, and a glossary of every invented term
(`queen`, `worker`, `supervisor`, `CEO`, the L0–L5 ladder, the transport seam),
each marked live / design-only / superseded.

Two traps it exists to prevent:

- **`docs/superpowers/plans/` is history.** Those are build instructions, executed
  once, accurate only on the day they ran. Do not read them to learn how skep works.
- **Specs are dated, and superseding is implicit.** `2026-07-09-l1-memory-substrate-design.md`
  is superseded by `2026-07-09-l1.1-...`; nothing in the directory says so.

`ARCHITECTURE.md` is hand-written and overwritten in place. Never file a dated copy
of it under `docs/`. When it disagrees with the code, the code wins — fix the file.
