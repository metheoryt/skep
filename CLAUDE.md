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

<!-- gortex:communities:start -->
<!-- gortex:skills:start -->
## Community Skills

| Area | Description | Skill |
|------|-------------|-------|
| 2 Dirs Fleetd Stream Event | 35 symbols | `/gortex-2-dirs-fleetd-stream-event` |
| Fleetd 1 Dirs Run Events | 32 symbols | `/gortex-fleetd-1-dirs-run-events` |
| 2 Dirs Fleetd Db Registry | 26 symbols | `/gortex-2-dirs-fleetd-db-registry` |
| 2 Dirs Magicmock | 24 symbols | `/gortex-2-dirs-magicmock` |
| 2 Dirs Path | 20 symbols | `/gortex-2-dirs-path` |
| Tests 1 Dirs | 19 symbols | `/gortex-tests-1-dirs` |
| Fleetd 1 Dirs Start | 17 symbols | `/gortex-fleetd-1-dirs-start` |
| 1 Dirs Fleetd Stream Parse Event | 15 symbols | `/gortex-1-dirs-fleetd-stream-parse-event` |
| 2 Dirs Build Dispatcher | 13 symbols | `/gortex-2-dirs-build-dispatcher` |
| Fleetd Task | 12 symbols | `/gortex-fleetd-task` |
| Fleetd 1 Dirs Kill | 11 symbols | `/gortex-fleetd-1-dirs-kill` |
| 1 Dirs Add Task | 9 symbols | `/gortex-1-dirs-add-task` |
| 2 Dirs Fleetd Telegram Gw Is Owner | 8 symbols | `/gortex-2-dirs-fleetd-telegram-gw-is-owner` |
| Fleetd Activity Line | 8 symbols | `/gortex-fleetd-activity-line` |
| 2 Dirs Run | 8 symbols | `/gortex-2-dirs-run` |
| Tests Killednoresultagent | 6 symbols | `/gortex-tests-killednoresultagent` |
| 1 Dirs Main Fake Claude | 6 symbols | `/gortex-1-dirs-main-fake-claude` |
| Tests Killmidstreamagent | 6 symbols | `/gortex-tests-killmidstreamagent` |
| 1 Dirs Main External Call Dep Fleetd Telegram Gw | 6 symbols | `/gortex-1-dirs-main-external-call-dep-fleetd-telegram-gw` |
| Tests Wt Factory | 4 symbols | `/gortex-tests-wt-factory` |
<!-- gortex:skills:end -->

<!-- gortex:communities:end -->
