# Sessions A3 — usage-limit park & auto-resume — design

**Sub-project A3 of Sessions.** Depends on A1 (worker-side invocations, merged) and
A2 (queen-side sessions, merged). A2 §10 named this slice as unblocked-but-deferred:
"`/resume <ref>` becomes buildable: the registry maps `ref → (host, profile,
session_local_id)`, which is exactly `Supervisor.resume`'s argument." This spec builds
the drive path A2 left dangling, and the one capability that justifies it.

## 1. The problem A3 actually solves

An unattended agent that hits an account usage limit **dies**. The runner emits an
error result, `run_events` records `terminal="failed"`, the topic goes quiet, and the
task is lost — even though the runner's own transcript is intact and would resume
cleanly minutes or hours later when the limit resets.

The Sessions model already makes resume *cheap*: skep never owns or replays a
transcript (`2026-07-10-sessions-design.md` §"skep never owns…"). The session is a
journal holding a pointer — `ref → (host, profile, resume_token, worktree)` — and
resume is just re-invoking `claude --resume <resume_token>`. A1 built the worker half
(`Supervisor.resume`); A2 built the journal (`Bookkeeping`, `by_session`,
`rebind_invocation`). **Nothing connects them, and nothing detects the limit.**

A3 closes exactly that: recognise a usage limit, **park** instead of fail, and
**auto-resume** when the limit resets — with no human in the loop. Manual `/resume
<ref>` falls out of the same path as an early-unpark override.

**Why this is the load-bearing slice, not the manual command.** A human-triggered
`/resume` is a convenience over `claude --resume` in a terminal (it re-rails output
onto the topic and routes to the right host). The *capability* that is impossible
without skep is the fleet surviving a usage limit while no one is watching. That
requires detection + a scheduler, which is what this spec centres on.

## 2. Scope

**In:**

1. A **`parked` terminal state** produced by a usage-limit **detector** on the worker.
2. **`parked_until`** on the queen journal (migration), and the parked state on the
   Telegram surface.
3. The **resume drive path**: `/resume <ref>` → `QueenRouter.cmd_resume` → a `resume`
   wire frame → worker → `Supervisor.resume` (exists).
4. A **queen-side sweep scheduler** that auto-resumes due-parked sessions.

**Out, with the reason:**

| Deferred | Why |
|---|---|
| **P2 — multi-account pool, switch-on-limit, switch-back-on-reset** | Rests on an unverified credential-injection mechanism (see §8.2). Its own spec, unblocked once the spike passes. On a single account, a limit means *wait for reset* — exactly what A3 does; switching accounts is the optimisation on top. |
| **P3 — model auto-select / per-subagent model** | Orthogonal to park/resume. `Supervisor.resume` already accepts `model=`; A3 threads it but sets no policy. |
| **`primary:rw` lease on resume** | A3 opens no `rw` root it does not already hold; the parked worktree is the same one the prior invocation used. No new lease to arbitrate. |
| **Topic-close ↔ park binding (E)** | Service-message semantics own the Telegram close/reopen gestures. A3 parks on the *runner's* limit signal, not a UI gesture. |
| **Cross-host park migration** | Usage limits are account-scoped (`2026-07-10` §risks): moving a parked session to another host does not dodge the limit. Resume stays on the originating (host, profile). |

## 3. The `parked` terminal state and its detector

**The one unknown, isolated behind a function.** Claude Code's exact usage-limit
payload is not yet captured (§8.1). A3 does not scatter guesses through `run_events`;
it puts the whole guess in one testable predicate:

```python
# stream.py
def usage_limit_reset(ev: Event) -> datetime | None:
    """Reset time if `ev` is a usage-limit result, else None."""
```

Best-guess implementation: on a `result` event with `is_error=True`, match the limit
text (e.g. "usage limit reached") and parse a reset time if present. A captured real
fixture (§8.1) hardens it later; tests drive it now via `fake_claude` emitting a
synthetic limit result. **The predicate is the only place that changes when the real
shape lands.**

**Wiring in `supervisor.run_events`.** On a `result`, before assigning
`terminal = "failed" if ev.is_error else "done"`:

```python
reset_at = usage_limit_reset(ev)
if reset_at is not None:
    terminal = "parked"           # resume_token is already persisted (existing lines 316/322)
    park_reset = reset_at         # carried to the sink in `finally`
```

The `finally` block already calls `self._sink.done(task_id, terminal, summary)`. A3
extends the sink:

```python
async def done(self, local_id: int, status: str, summary: str,
               reset_at: float | None = None) -> None: ...
```

`reset_at` is a POSIX timestamp (float), `None` for every non-parked terminal. All
`EventSink` implementations (the wire sink in `transport.py`, the Telegram sink) take
the new optional param; the wire frame for `done` carries it.

## 4. Queen journal: the parked state

`Bookkeeping` gains one column via the same additive migration shape A2 used for
`session_local_id`:

```sql
ALTER TABLE entries ADD COLUMN parked_until REAL   -- POSIX ts; NULL unless parked
```

- On a `done(status="parked", reset_at=T)`, the sink sets `status='parked',
  parked_until=T` on the entry. If `reset_at` is `None`, apply the default backoff
  (§7) rather than storing `NULL`.
- `rebind_invocation` (exists) already flips `status='running'` on resume; A3 clears
  `parked_until` there too.
- `Bookkeeping.parked_due(now) -> list[Entry]` — `status='parked' AND parked_until <=
  now`, the sweep's query.
- `format_ls` shows `parked`; the activity line reads e.g. `parked · resumes ~3:45pm`.

## 5. The resume drive path (mirrors `/spawn` exactly)

Every seam here has a `spawn` twin already in the tree; A3 adds the `resume` sibling.

1. **Telegram** `/resume <ref>` (optional `--model X`) → `QueenRouter.cmd_resume(ref,
   model=None)`.
2. **`cmd_resume`** looks up the entry, refuses if absent or not resumable, and
   **guards on `status`**: resume only if `status in {parked, done, failed, killed}`
   and never if already `running`. Then `handler.resume(entry.session_local_id,
   model)`.
3. **Transport** — `CommandHandler.resume(session_local_id, model)` (new Protocol
   method) and a `resume` wire frame carrying **ids/names only** (like the A2 spawn
   frame carried root *names*): `session_local_id`, optional `model`.
4. **Worker `app.py`** dispatches the frame to `Supervisor.resume(session_local_id,
   model=model)` — **exists** (supervisor.py:252). It creates a new invocation on the
   same `resume_token` + `worktree_path`, emits `task_started(…, session_local_id)`.
5. **`rebind_invocation(ref, new_local_id)`** — **exists** — rebinds the *same* topic
   and clears `parked_until`. Output streams back where it parked.

**The guard is the concurrency invariant.** Manual `/resume` and the sweep can race.
`cmd_resume` resumes only from a non-running status, and `rebind_invocation` flips to
`running` atomically. The first to rebind wins; the second sees `running` and no-ops.
No double invocation.

## 6. The sweep scheduler

Auto-resume is a **periodic sweep on the queen**, not an asyncio-timer-per-session.
The queen owns the journal and sees every parked session across all workers, so it is
the natural scheduler. It is created in `serve()` (the asyncio entrypoint,
queen/app.py:66) and cancelled on shutdown.

```python
async def _park_sweep(bk, router, interval, now):
    while True:
        for entry in bk.parked_due(now()):
            if not router.is_online(entry.host, entry.profile):
                continue                          # worker gone — next tick
            try:
                await router.cmd_resume(entry.ref)
            except CapacityError:
                continue                          # worker full — next tick
        await asyncio.sleep(interval)
```

A sweep — rather than per-session timers — makes every edge fall out of re-evaluation
instead of bespoke handling:

| Edge | Sweep behaviour |
|---|---|
| Worker offline at reset T | `is_online` false → skipped; resumed automatically the first tick after it reconnects |
| Queen restart | `parked_until` lives in the journal; the sweep just resumes finding due entries — no timer state to rebuild |
| Worker at capacity | `CapacityError` → skipped, retried next tick |
| Reset time guessed early | resume → runner re-hits the limit → re-parks with a fresh `parked_until` — self-correcting |
| Manual `/resume` races the sweep | §5 status guard: second caller sees `running`, no-ops |

Config (env, `QueenConfig`): `SKEP_PARK_SWEEP_INTERVAL` (default 30 s). The sweep is
idempotent and cheap — one indexed query per tick.

## 7. No reset time → default backoff

When `usage_limit_reset` recognises a limit but cannot parse a reset (unknown or
garbled text), the session **still parks**, with `parked_until = now + backoff`
(`SKEP_PARK_DEFAULT_BACKOFF`, default 3600 s). The sweep retries then; if still
limited the runner re-parks for another backoff. The session is never stranded and
never manual-only.

**Anti-tight-loop.** Every `parked_until` (parsed *or* defaulted) gets small positive
jitter (0–60 s) so a fleet that parked together does not resume in lockstep and
re-hit the same account limit simultaneously.

Only a *recognised* limit parks. An unrecognised error result still becomes
`failed` — A3 adds no new way to mask a genuine failure as a park.

## 8. Two empirical unknowns (day-1 capture tasks)

Both are isolated behind functions and fixture-driven in tests; neither blocks the
structural work, but the first must be captured before A3 is trustworthy in
production.

**8.1 — Usage-limit event shape (blocks A3 correctness).** Capture a real Claude Code
usage-limit result against live `claude` and pin `usage_limit_reset` to it. Until
captured, the predicate runs on a best-guess text match and a `fake_claude` fixture.
Risk if wrong: a real limit is mis-classified as `failed` (no worse than today) — it
does not corrupt state.

**8.2 — Credential-injection mechanism (blocks P2, not A3).** Separate spike: prove
per-spawn **env** credential injection (`ANTHROPIC_API_KEY` /
`CLAUDE_CODE_OAUTH_TOKEN`) works over an unmutated shared `~/.claude`, for both an
OAuth-token account and an API-key account, with two concurrent agents on different
accounts not interfering. Empirical finding driving this: the current `claude`
process (Orca-spawned) carries **no** credential env var — it auths from the config
dir — so Orca's own mechanism is unconfirmed and may be file-mutation (racy under
skep's concurrent spawns). A3 does not depend on the outcome; P2 does.

## 9. Risks and honest residuals

- **Detection is a text heuristic until §8.1 lands.** A provider wording change can
  silently turn a park back into a `failed`. Mitigation: the predicate is one
  function with one fixture; refreshing it is a one-line change.
- **`parked` is not `done`.** Consumers that treat any terminal as "finished" (metrics,
  cleanup) must special-case `parked` as *live-but-idle*. The session, its worktree,
  and its topic persist; only the process is gone.
- **Resume can re-hit the limit.** If the reset guess is early, the agent burns a
  round-trip re-parking. Bounded by backoff + jitter; not free, but self-correcting.
- **No mid-run preemption.** A3 parks only at a terminal `result`. A limit that
  manifests mid-stream without a terminal result is out of scope (matches
  `2026-07-10` §"v1 scope: model changes only at invocation boundaries").
- **Sweep latency.** Auto-resume fires within one sweep interval of the reset, not at
  the exact second. 30 s is well inside the granularity of an hour-scale limit.

## 10. What this unblocks

- **P2 (multi-account pool)** slots in at the same seam: on a recognised limit, if
  another account is available, switch credential and resume *immediately* (the shared
  transcript keeps context) instead of parking; park only when the whole pool is
  limited; the sweep's switch-back becomes "resume when the earliest account resets."
  A3's detector, `parked` state, and resume path are exactly its substrate.
- **P3 (per-subagent model)** rides the `model=` already threaded through `cmd_resume`
  → `Supervisor.resume`.
- The scheduler generalises: any future "resume this session at time T" (park-on-idle,
  scheduled kickoff) reuses `parked_until` + the sweep.
