from datetime import UTC, datetime

import pytest

from skep.memory import MemoryStore, memory_dir, parse_fact, write_memory


def _clock(iso: str = "2026-07-09T14:22:03Z"):
    return lambda: datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC)


def test_write_creates_dir_and_returns_path(tmp_path):
    p = write_memory(tmp_path, "Stack takes 90s", "poll /healthz", now=_clock())
    assert p == tmp_path / ".agent-memory" / "stack-takes-90s.md"
    assert p.exists()


def test_written_fact_is_always_readable(tmp_path):
    # The property §6's skip-on-malformed rule would otherwise hide.
    p = write_memory(tmp_path, 'Gotcha: he said "run \\ now"', "b", now=_clock())
    fact = parse_fact(p.stem, p.read_text())
    assert fact is not None
    assert fact.title == 'Gotcha: he said "run \\ now"'
    assert fact.created == "2026-07-09T14:22:03Z"


def test_newline_in_title_rejected(tmp_path):
    with pytest.raises(ValueError):
        write_memory(tmp_path, "two\nlines", "b", now=_clock())


def test_unknown_kind_rejected(tmp_path):
    with pytest.raises(ValueError):
        write_memory(tmp_path, "T", "b", kind="rumour", now=_clock())


def test_collision_gets_suffix_not_overwrite(tmp_path):
    a = write_memory(tmp_path, "Same Title", "first", now=_clock())
    b = write_memory(tmp_path, "Same Title", "second", now=_clock())
    assert a.name == "same-title.md"
    assert b.name == "same-title-2.md"
    assert "first" in a.read_text()


def test_third_collision_gets_three(tmp_path):
    write_memory(tmp_path, "T", "1", now=_clock())
    write_memory(tmp_path, "T", "2", now=_clock())
    c = write_memory(tmp_path, "T", "3", now=_clock())
    assert c.name == "t-3.md"


def test_supersedes_marks_old_and_leaves_it_on_disk(tmp_path):
    old = write_memory(tmp_path, "Old fact", "stale", now=_clock())
    new = write_memory(
        tmp_path, "New fact", "fresh", supersedes="old-fact", now=_clock()
    )
    old_fact = parse_fact(old.stem, old.read_text())
    assert old_fact.superseded_by == new.stem
    assert old.exists()


async def test_superseded_fact_stops_being_injected(tmp_path):
    write_memory(tmp_path, "Old fact", "stale", now=_clock())
    write_memory(tmp_path, "New fact", "fresh", supersedes="old-fact", now=_clock())
    out = await MemoryStore().addendum_for(tmp_path)
    assert "[old-fact]" not in out
    assert "[new-fact]" in out


def test_supersedes_nonexistent_is_rejected(tmp_path):
    # Creating a memory by superseding it is not a thing.
    with pytest.raises(ValueError):
        write_memory(tmp_path, "New", "b", supersedes="never-existed", now=_clock())
    assert not (tmp_path / ".agent-memory" / "never-existed.md").exists()


def test_supersedes_rejection_writes_nothing(tmp_path):
    with pytest.raises(ValueError):
        write_memory(tmp_path, "New", "b", supersedes="never-existed", now=_clock())
    assert not (tmp_path / ".agent-memory" / "new.md").exists()


# --- Path safety, run against BOTH attacker-controlled arguments (§5.1) ---

EVIL = ["../escape", "../../../.ssh/authorized_keys", "/etc/passwd", "!!!", ""]

# `title` runs through `slugify` before any containment check, so a
# traversal-shaped title is not proof of anything by itself: `slugify`
# neutralizes some inputs into ordinary, contained slugs (no `..`, no `/`
# survive), while others reduce to an empty string and are rejected outright.
# These two behaviors are different code paths and need different tests --
# asserting `pytest.raises` on a neutralized input proves nothing about the
# containment boundary (see task-4-report.md, Finding 2).
NEUTRALIZED_TITLES = [
    ("../escape", "escape"),
    ("../../../.ssh/authorized_keys", "ssh-authorized-keys"),
    ("/etc/passwd", "etc-passwd"),
]
REJECTED_TITLES = ["!!!", ""]


@pytest.mark.parametrize(("evil", "expected_slug"), NEUTRALIZED_TITLES)
def test_title_path_traversal_neutralized_and_contained(tmp_path, evil, expected_slug):
    """`slugify` reduces these traversal-shaped titles to ordinary slugs --
    they never reach the containment check as traversal attempts. The real
    property to check is that the write lands inside `.agent-memory/` and
    that the traversal target is never touched.
    """
    p = write_memory(tmp_path, evil, "b", now=_clock())
    assert p.parent == memory_dir(tmp_path).resolve()
    assert p.name == f"{expected_slug}.md"
    assert not (tmp_path.parent / ".ssh").exists()


@pytest.mark.parametrize("evil", REJECTED_TITLES)
def test_title_path_traversal_rejected_outright(tmp_path, evil):
    """These titles slugify to an empty string, so `slugify` itself raises --
    genuinely rejected, not merely neutralized.
    """
    with pytest.raises(ValueError):
        write_memory(tmp_path, evil, "b", now=_clock())
    assert not (tmp_path.parent / ".ssh").exists()


@pytest.mark.parametrize("evil", EVIL)
def test_supersedes_path_traversal_rejected(tmp_path, evil):
    write_memory(tmp_path, "Anchor", "b", now=_clock())
    with pytest.raises(ValueError):
        write_memory(tmp_path, "New", "b", supersedes=evil, now=_clock())


def test_supersedes_does_not_escape_via_symlink(tmp_path):
    write_memory(tmp_path, "Anchor", "b", now=_clock())
    outside = tmp_path / "outside.md"
    outside.write_text("do not touch")
    (tmp_path / ".agent-memory" / "sneaky.md").symlink_to(outside)
    with pytest.raises(ValueError):
        write_memory(tmp_path, "New", "b", supersedes="sneaky", now=_clock())
    assert outside.read_text() == "do not touch"


def test_title_does_not_escape_via_symlink(tmp_path):
    """Equivalent of test_supersedes_does_not_escape_via_symlink, for `title`.

    A pre-existing symlink sits exactly at the slug's *unsuffixed* target path
    (`.agent-memory/new.md`), pointing outside the store. `resolve_memory_file`
    resolves the symlink before comparing parents, so this must be caught by
    the base containment check -- the same one that guards `supersedes`.
    """
    outside = tmp_path / "outside.md"
    outside.write_text("do not touch")
    (tmp_path / ".agent-memory").mkdir()
    (tmp_path / ".agent-memory" / "new.md").symlink_to(outside)
    with pytest.raises(ValueError):
        write_memory(tmp_path, "New", "b", now=_clock())
    assert outside.read_text() == "do not touch"


def test_title_collision_suffix_does_not_escape_via_symlink(tmp_path):
    """The `-2` collision suffix is a NEW path string, built after the base
    slug already passed `resolve_memory_file`. If that suffixed candidate is
    never itself re-checked before the write, a symlink planted at the
    *suffixed* name -- not the base name -- escapes containment even though
    the base name is clean and legitimately occupied.

    Setup: `new.md` is a real, legitimately-written fact (so `_next_free`
    advances past it). `new-2.md` is a broken symlink pointing outside the
    store (a broken symlink so `Path.exists()` reports it as free, letting
    `_next_free` select it as the write target).
    """
    write_memory(tmp_path, "New", "first", now=_clock())
    outside = tmp_path / "outside.md"
    assert not outside.exists()
    (tmp_path / ".agent-memory" / "new-2.md").symlink_to(outside)
    with pytest.raises(ValueError):
        write_memory(tmp_path, "New", "second", now=_clock())
    assert not outside.exists()
