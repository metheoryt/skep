# Sessions A2 — queen-side sessions — design

**Sub-project A2 of Sessions.** Depends on A1 (worker-side invocations, merged).
Reads `docs/superpowers/specs/2026-07-10-sessions-design.md` as its parent spec; this
document narrows that spec's A2 scope to a landable increment and records what was
deliberately left out.

---

## 1. The problem A2 actually solves

A1 shipped capability, not behavior. `Supervisor.spawn_workspace` (multi-root,
`--model`), `Supervisor.resume`, and the `Workspace`/`Root` value types are all
reachable only from Python — nothing on the wire can drive them:

- `wire.spawn_msg` carries `repo` and `task`, nothing else.
- `RemoteWorker.spawn` sends exactly that frame.
- `WorkerWsClient._on_command` has a `SPAWN` branch that calls the two-argument
  `Supervisor.spawn`, and no resume branch at all.

Meanwhile the queen has no session concept: `Bookkeeping.entries` is one row per
*task*, so a second invocation of the same session would mint a second `ref` and a
second Telegram topic. The optional `session_local_id` A1 added to `task_started` is
accepted and discarded (`transport.py:69`).

A2 closes both: the queen learns what a session is, and one concrete multi-root
behavior becomes drivable from Telegram.

**The chosen first observable behavior** is the parent spec's §6 canonical pattern:
an agent works in its own fresh worktree while *watching the repo's main checkout
read-only*, including uncommitted changes. A detached worktree snapshot cannot show
uncommitted work; a read-only second root is the only way. It exercises multi-root
end to end, and it needs no lease — `primary:ro` is leaseless by the parent spec's
§6 lease rule.

---

## 2. Scope

**In:**

1. Session registry on the queen — `session_local_id` in `Bookkeeping`, ref/topic
   reuse across invocations, `session_local_id` threaded through register-replay.
2. Queen→worker drive path for a resolved workspace — a `roots` field on the spawn
   frame, carrying **names only**.
3. Worker-side root resolution and its refusals.
4. `/spawn … --watch` on the Telegram surface.
5. A read-only declaration in the spawn addendum, and rw-only memory writes.

**Out, with the reason:**

| Deferred | Why |
|---|---|
| `primary:rw` lease table | Nothing in A2 opens a `primary:rw` root — A2 refuses the mode outright. Building enforcement with no caller repeats the A1 mistake. |
| Parking / resume state machine, `/resume` | Its own slice. Needs the registry this spec builds, so it is unblocked but not included. |
| `visible` / `spawn_visibility` enforcement, hidden-`failed`-surfaces | These govern *children*, and children arrive with D (session spawning). |
| C (fleet catalog), `workspaces.yaml` | §5 resolves names on the worker instead. |
| Topic close/reopen ↔ park/resume binding | E owns the service-message semantics. |
| Worker/queen version negotiation | skep has no deployments; there is no skew to negotiate. |

---

## 3. Session registry

`Bookkeeping.entries` gains one column:

```sql
session_local_id INTEGER
```

**Migration.** `PRAGMA user_version`, mirroring the idiom `db.py::_migrate`
established for `Registry` in A1: at version 0, `ALTER TABLE entries ADD COLUMN
session_local_id INTEGER`, then `UPDATE entries SET session_local_id = local_id`
(every existing row is a one-invocation session), then bump. Idempotent on re-open.
The dev host carries a live `bookkeeping.sqlite` with real rows, so this is a real
migration, not a formality.

**Lookup.** New `by_session(host, profile, session_local_id) -> Entry | None`.
`by_worker_task` is unchanged and still correct: `local_id` remains unique per
*invocation*, so activity/milestone/done routing needs no change.

**Ref and topic reuse — this is topic-follows-session.** `QueenInbox.on_task_started`
gains a `session_local_id` parameter (the WS server already receives the field on the
frame and drops it). `QueenSink.on_task_started` becomes:

- Same invocation already known (`by_worker_task`) → return, as today.
- Else `session_local_id` is not `None` and `by_session(...)` finds a row → reuse that
  row's `ref` and `topic_id`, repoint `local_id` at the new invocation, set status back
  to `running`. **Critically, this branch runs before `create_topic`** — today the topic
  is created and then passed into `add()`, so reuse must short-circuit the creation, not
  just the insert.
- Else → create the topic and insert, storing `session_local_id` (falling back to the
  new row's own `local_id` when the field is absent).

The `None` fallback is one defensive line — `session_local_id` is typed optional from
A1 and both in-process and WS sinks may omit it.

**Honest status of the reuse branch: it has no live caller in A2.** The only way to
produce a second `task_started` for a known session is a resume, which A2 does not
build. Reconnect-replay does *not* exercise it — `QueenSink.on_task_started` already
returns early on a `by_worker_task` hit (`telegram_sink.py:25`), so replaying the same
invocation is already idempotent and does not mint a fresh ref. This branch is
forward-looking plumbing for `/resume`, built now because the registry column and the
migration are being added anyway. It is tested directly, not through a live path.

**Register-replay.** `WorkerWsClient._active_payload` (`ws_transport.py:295`) does not
carry `session_local_id`. That is harmless today for the reason just given, but it means
the replayed rows would be sessionless the moment resume exists — the gap the A1
whole-branch review flagged. A2 adds the field to the payload and passes it through the
replay loop in `QueenWsServer._handle`. The heartbeat payload shares the builder and
gets it for free.

---

## 4. Drive path

`wire.spawn_msg` gains an optional field:

```python
{"t": "spawn", "repo": ..., "task": ..., "roots": [{"name": ..., "mode": ..., "access": ...}]}
```

**`roots` carries names, never paths.** This is a security property, not a
convenience: `--add-dir` is an arbitrary-filesystem-read primitive, and a wire format
that accepts absolute paths hands a rogue or compromised queen a read primitive over
every worker's disk. The parent spec's §6 states the rule ("sessions and agents
reference workspaces by name, never by path"); A2 enforces it structurally by never
giving the wire a place to put a path.

`roots` absent means today's frame. That path stays live regardless of deployments —
`Supervisor.spawn(repo, task)` and the single-process `app.py` wiring both use it.

`CommandHandler.spawn` and `RemoteWorker.spawn` gain the optional argument;
`WorkerWsClient._on_command` builds a `Workspace` and calls `spawn_workspace`.

---

## 5. Root resolution on the worker

New `worker/roots.py::resolve_roots(cfg, specs) -> Workspace`. The worker owns
name→path resolution, exactly as `Supervisor.spawn` does today
(`self._cfg.repos_root / repo`). C is deferred whole.

This function is the security gate. It **refuses** — raising, never downgrading:

| Refusal | Reason |
|---|---|
| A name containing a path separator, or `..`, or an absolute path | Path traversal out of `repos_root`; the whole point of names-on-the-wire. |
| An unknown `mode` or `access` | Fail closed on a field skep does not understand. |
| `primary` + `rw` | The parent spec requires a queen-held lease for exactly this combination, and A2 does not build one. |
| `attach` | There is no shared-worktree registry yet; nothing can validate the `attach_ref`. |
| A head root whose mode is not `new` | A `ro` head root is useless (the agent could not write its own work) and a `primary:rw` head is refused above. |

Refusals reach the CEO through the existing `spawn_rejected` frame — the same path
`CapacityError` already uses.

Resolution itself: `new` → `repos_root/<name>` as the *parent* repo, from which
`Supervisor.spawn_workspace` creates `worktrees_root/<name>-<tid>`; `primary` →
`repos_root/<name>`, used as-is.

---

## 6. Telegram surface

`parse_spawn` gains a `--watch` flag:

```
/spawn <host> <profile> <repo> --watch <task>
```

which produces:

```python
[Root(repo, mode="new",     access="rw"),
 Root(repo, mode="primary", access="ro")]
```

**Opt-in, not default.** Every agent seeing the CEO's working tree means seeing
uncommitted work, local `.env` files, and whatever else lives in a checkout that is
never committed. Widening an agent's read surface is a deliberate act. Without
`--watch`, spawn behavior is byte-identical to today.

---

## 7. Read-only roots and the write paths that ignore them

`access="ro"` is advisory (§9). Two write paths must nonetheless respect it, because
they are skep's own and cost nothing to fix:

**Memory shim.** `supervisor.py:137` currently builds `roots = [(r.name, r.path) for r
in workspace.roots]` and hands *all* of them to `memory_shim_server`. Since `remember`
takes a `project` argument selecting a root by name, an agent could write
`.agent-memory/` files into the CEO's live checkout. A2 passes **rw roots only** to the
shim. Reading is unaffected: `MemoryProvider.addendum_for` continues to union every
root's store, `ro` included — reading a `ro` root is the entire point.

**Implementation trap:** that one `roots` variable feeds *both* the addendum read
(`supervisor.py:141`) and the shim (`:151`). The fix is two lists, not a narrowed one —
narrowing in place would silently starve the addendum of the watched root's memory,
which is exactly the thing the CEO wanted the agent to see.

**Spawn addendum.** The addendum gains a declaration naming each `ro` root: read
freely, do not write, and do not run branch operations (`checkout`, `reset`, `stash`,
`rebase`) there. `Supervisor` composes `append_system_prompt` from the memory addendum
and this declaration, either of which may be absent.

---

## 8. Testing

- **Migration:** an `entries` table at version 0 with rows gains the column and
  backfills `session_local_id = local_id`; re-opening is idempotent.
- **Registry:** a second `task_started` with the same `(host, profile,
  session_local_id)` reuses `ref` and `topic_id` and repoints `local_id`; a `None`
  `session_local_id` inserts; `by_worker_task` still resolves each invocation.
- **Replay:** `_active_payload` round-trips `session_local_id`, and a reconnect
  replay reuses refs rather than minting them.
- **Wire:** `spawn_msg` codec with and without `roots`.
- **Resolver:** each refusal in §5's table, plus `primary:ro` resolving to
  `repos_root/<name>`.
- **End to end:** `/spawn … --watch` reaches an `AgentProcess` argv containing
  `--add-dir <repos_root>/<repo>`, with `cwd` still the fresh worktree.
- **Memory:** the shim's root map excludes `ro` roots; the addendum still unions them.
- **Addendum:** contains the read-only declaration when a `ro` root is present, and
  does not when none is.

---

## 9. Risks

- **`ro` is advisory.** The agent has `Bash`; skep declares the constraint to a
  cooperative agent and closes its own two write paths (§7). Real enforcement is
  Phase 4 (sandbox). Stated plainly so the declaration is not mistaken for a sandbox.
- **A lockless reader can observe a torn tree.** If the CEO runs `checkout` or
  `rebase` in the primary checkout mid-read, the agent sees a half-switched directory
  — not corruption, but possibly wrong answers.
- **Read-surface widening.** A watched checkout may hold uncommitted secrets. Opt-in
  (§6) is the mitigation; there is no technical one before Phase 4.
- **Ref reuse changes what a `ref` means.** After A2 a `ref` names a session, not a
  task. `/kill <ref>` still resolves through `local_id`, which now points at the
  *latest* invocation — correct, but worth stating, since a future parked-session
  kill has no live invocation to target.

---

## 10. What this unblocks

`/resume <ref>` becomes buildable: the registry maps `ref → (host, profile,
session_local_id)`, which is exactly `Supervisor.resume`'s argument. Parking, the
`primary:rw` lease, and D's child sessions all build on the same map.
