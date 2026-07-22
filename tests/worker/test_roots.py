from pathlib import Path

import pytest

from skep.worker.roots import RootError, resolve_roots

REPOS = Path("/repos")


def test_single_new_root_resolves_under_repos_root():
    ws = resolve_roots(REPOS, [{"name": "nix", "mode": "new", "access": "rw"}])
    assert ws.roots[0].path == REPOS / "nix"
    assert ws.roots[0].mode == "new"
    assert ws.roots[0].access == "rw"
    assert ws.add_dir_paths == []


def test_watch_pattern_resolves_to_two_roots():
    ws = resolve_roots(
        REPOS,
        [
            {"name": "nix", "mode": "new", "access": "rw"},
            {"name": "nix", "mode": "primary", "access": "ro"},
        ],
    )
    assert ws.primary_path == REPOS / "nix"
    assert ws.add_dir_paths == [REPOS / "nix"]
    assert ws.requires_lease is False


def test_mode_and_access_default_when_omitted():
    ws = resolve_roots(REPOS, [{"name": "nix"}])
    assert (ws.roots[0].mode, ws.roots[0].access) == ("new", "rw")


@pytest.mark.parametrize(
    "name",
    ["../etc", "a/b", "/etc", "..", "nix/../..", "a\\b", ""],
)
def test_names_that_could_escape_repos_root_are_refused(name):
    with pytest.raises(RootError):
        resolve_roots(REPOS, [{"name": name}])


def test_primary_rw_is_refused_pending_the_lease():
    with pytest.raises(RootError, match="lease"):
        resolve_roots(
            REPOS,
            [
                {"name": "nix", "mode": "new", "access": "rw"},
                {"name": "nix", "mode": "primary", "access": "rw"},
            ],
        )


def test_attach_is_refused():
    with pytest.raises(RootError, match="attach"):
        resolve_roots(
            REPOS,
            [
                {"name": "nix", "mode": "new", "access": "rw"},
                {"name": "other", "mode": "attach", "access": "rw"},
            ],
        )


def test_head_root_must_be_new():
    with pytest.raises(RootError, match="head"):
        resolve_roots(REPOS, [{"name": "nix", "mode": "primary", "access": "ro"}])


def test_unknown_mode_or_access_is_refused():
    with pytest.raises(RootError):
        resolve_roots(REPOS, [{"name": "nix", "mode": "teleport"}])
    with pytest.raises(RootError):
        resolve_roots(REPOS, [{"name": "nix", "access": "wx"}])


def test_empty_spec_list_is_refused():
    with pytest.raises(RootError):
        resolve_roots(REPOS, [])


def test_symlink_escape_under_repos_root_is_refused(tmp_path):
    # A bare name (no "/", no "..") passes the string filter, but the entry
    # it names inside repos_root can still be a symlink pointing outside
    # repos_root -- only a real filesystem resolve() catches that.
    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repos_root / "evil").symlink_to(outside)

    with pytest.raises(RootError, match="escapes"):
        resolve_roots(repos_root, [{"name": "evil", "mode": "new", "access": "rw"}])


def test_non_dict_spec_entry_is_refused():
    # Root specs are attacker-controlled JSON off the wire -- nothing
    # guarantees every element of the list is an object.
    with pytest.raises(RootError):
        resolve_roots(REPOS, ["nix"])


def test_name_with_embedded_nul_is_refused():
    # JSON strings may contain a NUL; Path.resolve() raises ValueError on
    # one, which must not escape resolve_roots as a bare ValueError.
    with pytest.raises(RootError):
        resolve_roots(REPOS, [{"name": "ni\x00x", "mode": "new", "access": "rw"}])
