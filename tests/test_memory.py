import pytest

from skep.memory import memory_dir, resolve_memory_file, slugify


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
