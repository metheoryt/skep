from pathlib import Path

import pytest

from skep.workspace import ACCESS_RO, MODE_NEW, MODE_PRIMARY, Root, Workspace


def test_single_root_default_is_new_rw():
    ws = Workspace.single("nix", Path("/repos/nix"))
    assert ws.roots[0].mode == MODE_NEW
    assert ws.roots[0].access == "rw"
    assert ws.add_dir_paths == []
    assert ws.requires_lease is False
    assert ws.primary_path == Path("/repos/nix")


def test_multi_root_renders_cwd_and_add_dirs():
    ws = Workspace(
        roots=[
            Root("nix", Path("/wt/nix-1"), mode=MODE_NEW),
            Root("main", Path("/repos/main"), mode=MODE_PRIMARY, access=ACCESS_RO),
        ]
    )
    assert ws.primary_path == Path("/wt/nix-1")
    assert ws.add_dir_paths == [Path("/repos/main")]
    assert ws.requires_lease is False       # primary:ro needs no lease


def test_primary_rw_requires_lease():
    ws = Workspace(roots=[Root("main", Path("/repos/main"), mode=MODE_PRIMARY)])
    assert ws.requires_lease is True


def test_empty_workspace_rejected():
    with pytest.raises(ValueError):
        Workspace(roots=[])
