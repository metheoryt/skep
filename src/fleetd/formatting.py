from __future__ import annotations

from fleetd.stream import Event

_MD_RESERVED = r"_*[]()~`>#+-=|{}.!"
_MAX_ACTIVITY = 200


def escape_md(text: str) -> str:
    out = []
    for ch in text:
        if ch in _MD_RESERVED:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _truncate(text: str) -> str:
    if len(text) <= _MAX_ACTIVITY:
        return text
    return text[: _MAX_ACTIVITY - 1] + "…"


def activity_line(event: Event) -> str | None:
    if event.kind == "assistant_text":
        return _truncate("💬 " + escape_md(event.text))
    if event.kind == "tool_use":
        return _truncate("🔧 " + escape_md(event.tool_name))
    return None


def milestone_message(event: Event) -> str | None:
    if event.kind != "result":
        return None
    if event.is_error:
        return "❌ Failed: " + event.text
    return "✅ Done: " + event.text
