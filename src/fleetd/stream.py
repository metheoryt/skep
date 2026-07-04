from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Event:
    kind: str
    text: str = ""
    tool_name: str = ""
    session_id: str | None = None
    is_error: bool = False
    raw: dict = field(default_factory=dict)


def _first_blocks(obj: dict) -> list[dict]:
    content = (obj.get("message") or {}).get("content", [])
    return content if isinstance(content, list) else []


def parse_event(line: str) -> Event | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
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
