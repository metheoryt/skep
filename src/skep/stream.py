from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    kind: str
    text: str = ""
    tool_name: str = ""
    session_id: str | None = None
    is_error: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class UsageLimit:
    """A recognised usage limit. `reset_at` is a POSIX ts, or None if the
    runner gave no parseable reset (caller applies a default backoff)."""

    reset_at: float | None


# Best-guess text match. Hardened by a captured real fixture (design section 8.1);
# this is the ONLY place that changes when the real event shape lands.
_LIMIT_MARKERS = ("usage limit reached", "usage limit exceeded")


def detect_usage_limit(ev: Event) -> UsageLimit | None:
    if ev.kind != "result" or not ev.is_error:
        return None
    text = (ev.text or "").lower()
    raw = ev.raw or {}
    subtype = str(raw.get("subtype", "")).lower()
    is_limit = subtype == "usage_limit" or any(m in text for m in _LIMIT_MARKERS)
    if not is_limit:
        return None
    reset_raw = raw.get("reset_at")
    reset_at = float(reset_raw) if isinstance(reset_raw, (int, float)) else None
    return UsageLimit(reset_at=reset_at)


def _first_blocks(obj: dict[str, Any]) -> list[dict[str, Any]]:
    content = (obj.get("message") or {}).get("content", [])
    return content if isinstance(content, list) else []


def parse_event(line: str) -> Event | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj: dict[str, Any] = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    etype = obj.get("type")
    if etype == "system":
        return Event(kind="system", session_id=obj.get("session_id"), raw=obj)
    if etype == "result":
        return Event(
            kind="result",
            text=obj.get("result", "") or "",
            session_id=obj.get("session_id"),
            is_error=bool(obj.get("is_error", False)),
            raw=obj,
        )
    if etype == "assistant":
        for block in _first_blocks(obj):
            if block.get("type") == "text":
                return Event(kind="assistant_text", text=block.get("text", ""), raw=obj)
            if block.get("type") == "tool_use":
                return Event(kind="tool_use", tool_name=block.get("name", ""), raw=obj)
        return Event(kind="unknown", raw=obj)
    if etype == "user":
        for block in _first_blocks(obj):
            if block.get("type") == "tool_result":
                return Event(kind="tool_result", raw=obj)
        return Event(kind="unknown", raw=obj)
    return Event(kind="unknown", raw=obj)
