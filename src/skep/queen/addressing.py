"""L0 Mailbox address resolution: ceo / mgr:<name> / <ref>."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# An IC agent is addressable only while active. Mirrors bookkeeping's active
# states; any terminal (done/failed/killed) or unknown status is non-addressable
# so mail is never routed to a stopped agent (fail closed).
_ADDRESSABLE_STATUSES = frozenset({"spawning", "running"})


class _Entryish(Protocol):
    status: str


class _Bookkeepingish(Protocol):
    def get(self, ref: int) -> _Entryish | None: ...


@dataclass
class Resolution:
    kind: str  # "ceo" | "mgr" | "ic" | "invalid"
    ref: int | None
    canonical: str
    error: str | None


def resolve_address(
    to: str,
    bookkeeping: _Bookkeepingish,
    managers: set[str],
) -> Resolution:
    to = to.strip()
    if to == "ceo":
        return Resolution(kind="ceo", ref=None, canonical="ceo", error=None)

    if to.startswith("mgr:"):
        name = to[len("mgr:") :]
        if name in managers:
            return Resolution(kind="mgr", ref=None, canonical=f"mgr:{name}", error=None)
        return Resolution(
            kind="invalid", ref=None, canonical=to, error=f"unknown manager '{name}'"
        )

    if to.isdigit():
        ref = int(to)
        entry = bookkeeping.get(ref)
        if entry is None:
            return Resolution(
                kind="invalid", ref=None, canonical=to, error=f"no such agent ref {ref}"
            )
        if entry.status not in _ADDRESSABLE_STATUSES:
            return Resolution(
                kind="invalid",
                ref=None,
                canonical=to,
                error=f"agent {ref} is not active ({entry.status})",
            )
        return Resolution(kind="ic", ref=ref, canonical=str(ref), error=None)

    return Resolution(
        kind="invalid", ref=None, canonical=to, error=f"unrecognized address '{to}'"
    )
