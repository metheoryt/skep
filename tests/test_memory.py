import pytest

from skep.memory import (
    KINDS,
    MemoryFact,
    memory_dir,
    parse_fact,
    resolve_memory_file,
    serialize_fact,
    slugify,
)


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Stack Takes 90s To Come Up") == "stack-takes-90s-to-come-up"


def test_slugify_collapses_runs_and_strips_edges():
    assert slugify("  --Hello,,,   World!!  ") == "hello-world"


def test_slugify_rejects_empty_result():
    with pytest.raises(ValueError):
        slugify("!!!")


def test_slugify_rejects_empty_input():
    with pytest.raises(ValueError):
        slugify("")


def test_slugify_neutralizes_traversal():
    # "../../../.ssh/authorized_keys" must not survive as path syntax.
    slug = slugify("../../../.ssh/authorized_keys")
    assert "/" not in slug and ".." not in slug


def test_memory_dir_is_dot_agent_memory(tmp_path):
    assert memory_dir(tmp_path) == tmp_path / ".agent-memory"


def test_resolve_memory_file_inside_dir(tmp_path):
    p = resolve_memory_file(tmp_path, "ws-reconnect-needs-jitter")
    assert p == (tmp_path / ".agent-memory" / "ws-reconnect-needs-jitter.md").resolve()


@pytest.mark.parametrize(
    "evil",
    [
        "../escape",
        "../../.ssh/authorized_keys",
        "/etc/passwd",
        "a/b",
        "",
        ".",
        "..",
    ],
)
def test_resolve_memory_file_rejects_escapes(tmp_path, evil):
    # Step 3's containment assert must hold even for inputs a slugifier
    # might one day be "improved" to allow.
    with pytest.raises(ValueError):
        resolve_memory_file(tmp_path, evil)


def test_resolve_memory_file_rejects_symlink_escape(tmp_path):
    mem = tmp_path / ".agent-memory"
    mem.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (mem / "sneaky.md").symlink_to(outside / "target.md")
    # resolve() follows the symlink; the target is outside the memory dir.
    with pytest.raises(ValueError):
        resolve_memory_file(tmp_path, "sneaky")


def _fact(**kw) -> MemoryFact:
    base = dict(
        slug="stack-takes-90s",
        title="Stack takes 90s to come up",
        kind="gotcha",
        created="2026-07-09T14:22:03Z",
        superseded_by=None,
        body="`docker compose up` returns before the API is healthy.",
    )
    base.update(kw)
    return MemoryFact(**base)


def test_round_trip_plain():
    f = _fact()
    assert parse_fact(f.slug, serialize_fact(f)) == f


def test_round_trip_title_with_colon():
    # The failure §3.1 exists to prevent: a naive partition(":") reader loses this.
    f = _fact(title="Gotcha: stack takes 90s")
    got = parse_fact(f.slug, serialize_fact(f))
    assert got is not None and got.title == "Gotcha: stack takes 90s"


def test_round_trip_title_with_quotes_and_backslash():
    f = _fact(title='He said "run \\ now"')
    got = parse_fact(f.slug, serialize_fact(f))
    assert got is not None and got.title == 'He said "run \\ now"'


def test_round_trip_superseded_by():
    f = _fact(superseded_by="newer-slug")
    got = parse_fact(f.slug, serialize_fact(f))
    assert got is not None and got.superseded_by == "newer-slug"


def test_round_trip_multiline_body():
    f = _fact(body="line one\n\nline three\n")
    got = parse_fact(f.slug, serialize_fact(f))
    assert got is not None and got.body == "line one\n\nline three"


def test_serialize_rejects_newline_in_title():
    with pytest.raises(ValueError):
        serialize_fact(_fact(title="two\nlines"))


def test_serialize_rejects_unknown_kind():
    with pytest.raises(ValueError):
        serialize_fact(_fact(kind="rumour"))


def test_kinds_is_the_closed_vocabulary():
    assert KINDS == frozenset(
        {"gotcha", "constraint", "decision", "convention", "incident"}
    )


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter at all",
        "---\ntitle: unquoted\n---\nbody",  # title must be a quoted scalar
        '---\ntitle: "x"\n---',  # missing kind/created
        '---\ntitle: "x"\nkind: "bogus"\ncreated: "2026-01-01T00:00:00Z"\n---\nb',
        '---\ntitle: "x"\nkind: gotcha\n',  # unterminated frontmatter
        "",
    ],
)
def test_parse_fact_returns_none_on_malformed(text):
    # §6: skip that file, keep the rest. Never raise.
    assert parse_fact("some-slug", text) is None
