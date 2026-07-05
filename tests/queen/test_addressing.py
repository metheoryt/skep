from dataclasses import dataclass

from skep.queen.addressing import resolve_address, Resolution


@dataclass
class _Entry:
    ref: int
    status: str


class _FakeBk:
    def __init__(self, entries: dict[int, _Entry]) -> None:
        self._e = entries

    def get(self, ref: int):
        return self._e.get(ref)


def test_ceo():
    r = resolve_address("ceo", _FakeBk({}), managers=set())
    assert r == Resolution(kind="ceo", ref=None, canonical="ceo", error=None)


def test_known_manager():
    r = resolve_address("mgr:alice", _FakeBk({}), managers={"alice"})
    assert r == Resolution(kind="mgr", ref=None, canonical="mgr:alice",
                           error=None)


def test_unknown_manager_invalid():
    r = resolve_address("mgr:ghost", _FakeBk({}), managers={"alice"})
    assert r.kind == "invalid"
    assert "ghost" in r.error


def test_ic_active_ref():
    bk = _FakeBk({3: _Entry(ref=3, status="running")})
    r = resolve_address("3", bk, managers=set())
    assert r == Resolution(kind="ic", ref=3, canonical="3", error=None)


def test_ic_done_is_invalid():
    bk = _FakeBk({3: _Entry(ref=3, status="done")})
    r = resolve_address("3", bk, managers=set())
    assert r.kind == "invalid"


def test_ic_missing_is_invalid():
    r = resolve_address("7", _FakeBk({}), managers=set())
    assert r.kind == "invalid"


def test_garbage_invalid():
    r = resolve_address("not-an-address", _FakeBk({}), managers=set())
    assert r.kind == "invalid"
