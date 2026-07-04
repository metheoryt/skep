from fleetd.db import Registry, Task


def test_add_and_get_task():
    reg = Registry.open(":memory:")
    tid = reg.add_task("nix", "clean nvidia", "/wt/nix-1")
    task = reg.get_task(tid)
    assert task.id == tid
    assert task.repo == "nix"
    assert task.task == "clean nvidia"
    assert task.worktree_path == "/wt/nix-1"
    assert task.mode == "native"
    assert task.status == "spawning"
    assert task.created_at  # non-empty ISO timestamp


def test_update_fields():
    reg = Registry.open(":memory:")
    tid = reg.add_task("nix", "t", "/wt/1")
    reg.update(tid, status="running", pid=999, session_id="sess-1", topic_id=7)
    task = reg.get_task(tid)
    assert (task.status, task.pid, task.session_id, task.topic_id) == (
        "running", 999, "sess-1", 7,
    )


def test_list_active_excludes_terminal():
    reg = Registry.open(":memory:")
    a = reg.add_task("r", "a", "/wt/a")
    b = reg.add_task("r", "b", "/wt/b")
    reg.update(a, status="running")
    reg.update(b, status="done")
    active = reg.list_active()
    assert [t.id for t in active] == [a]


def test_get_missing_returns_none():
    reg = Registry.open(":memory:")
    assert reg.get_task(123) is None


def test_audit_log_persists_rows():
    reg = Registry.open(":memory:")
    tid = reg.add_task("r", "t", "/wt/1")
    reg.log_audit(tid, "spawn", "claude -p ...")
    reg.log_audit(None, "panic", "fleet-wide kill")
    rows = reg.audit_rows()
    assert [(r["kind"], r["detail"]) for r in rows] == [
        ("spawn", "claude -p ..."),
        ("panic", "fleet-wide kill"),
    ]
