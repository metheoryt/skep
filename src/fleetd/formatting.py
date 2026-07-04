from __future__ import annotations

from fleetd.stream import Event

_MD_RESERVED = "\\_*[]()~`>#+-=|{}.!"
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
    cut = text[: _MAX_ACTIVITY - 1]
    # don't strand a lone escaping backslash across the cut
    trailing = len(cut) - len(cut.rstrip("\\"))
    if trailing % 2 == 1:
        cut = cut[:-1]
    return cut + "…"


def activity_line(event: Event) -> str | None:
    if event.kind == "assistant_text":
        return _truncate("💬 " + event.text)
    if event.kind == "tool_use":
        return _truncate("🔧 " + event.tool_name)
    return None


def milestone_message(event: Event) -> str | None:
    if event.kind != "result":
        return None
    if event.is_error:
        return "❌ Failed: " + event.text
    return "✅ Done: " + event.text
