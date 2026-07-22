import pytest

from skep import wire


def test_encode_decode_roundtrip():
    msg = wire.task_started_msg(7, "nix", "clean nvidia")
    assert wire.decode(wire.encode(msg)) == msg
    assert msg["t"] == wire.TASK_STARTED


def test_register_msg_shape():
    msg = wire.register_msg("g16", "work", "0.1.0",
                            [{"local_id": 1, "repo": "nix", "title": "t"}])
    assert msg == {
        "t": wire.REGISTER, "host": "g16", "profile": "work",
        "version": "0.1.0",
        "active_tasks": [{"local_id": 1, "repo": "nix", "title": "t"}],
    }


def test_all_builders_carry_a_tag():
    builders = [
        wire.heartbeat_msg([], 8),
        wire.activity_msg(1, "x"),
        wire.milestone_msg(1, "m"),
        wire.done_msg(1, "done", "ok"),
        wire.spawn_rejected_msg("at capacity"),
        wire.spawn_msg("nix", "task"),
        wire.kill_msg(3),
        wire.panic_msg(),
        wire.ls_request_msg(),
    ]
    for b in builders:
        assert "t" in b
        assert wire.decode(wire.encode(b)) == b


def test_decode_rejects_non_dict():
    with pytest.raises(ValueError):
        wire.decode("[1, 2, 3]")


def test_decode_rejects_missing_tag():
    with pytest.raises(ValueError):
        wire.decode('{"local_id": 1}')


def test_spawn_msg_carries_roots():
    roots = [
        {"name": "nix", "mode": "new", "access": "rw"},
        {"name": "nix", "mode": "primary", "access": "ro"},
    ]
    msg = wire.decode(wire.encode(wire.spawn_msg("nix", "t", roots)))
    assert msg["roots"] == roots


def test_spawn_msg_roots_default_to_none():
    msg = wire.decode(wire.encode(wire.spawn_msg("nix", "t")))
    assert msg.get("roots") is None
