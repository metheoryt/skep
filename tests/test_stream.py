import json

from skep.stream import Event, parse_event


def test_blank_line_returns_none():
    assert parse_event("   ") is None
    assert parse_event("") is None


def test_non_json_returns_none():
    assert parse_event("not json at all") is None


def test_system_init_carries_session_id():
    line = json.dumps({"type": "system", "subtype": "init", "session_id": "s1"})
    ev = parse_event(line)
    assert ev.kind == "system"
    assert ev.session_id == "s1"


def test_assistant_text_block():
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Hello there"}]},
    })
    ev = parse_event(line)
    assert ev.kind == "assistant_text"
    assert ev.text == "Hello there"


def test_assistant_tool_use_block():
    line = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "edit_file", "input": {"path": "a.py"}},
        ]},
    })
    ev = parse_event(line)
    assert ev.kind == "tool_use"
    assert ev.tool_name == "edit_file"


def test_user_tool_result_block():
    line = json.dumps({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "ok"}]},
    })
    ev = parse_event(line)
    assert ev.kind == "tool_result"


def test_result_event_captures_error_and_session():
    line = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "done", "session_id": "s1",
    })
    ev = parse_event(line)
    assert ev.kind == "result"
    assert ev.is_error is False
    assert ev.text == "done"
    assert ev.session_id == "s1"


def test_unknown_type_is_unknown_kind():
    ev = parse_event(json.dumps({"type": "weird"}))
    assert ev.kind == "unknown"


def test_assistant_message_null_does_not_crash():
    line = json.dumps({"type": "assistant", "message": None})
    ev = parse_event(line)
    assert ev is not None
    assert ev.kind == "unknown"


def test_user_message_null_does_not_crash():
    line = json.dumps({"type": "user", "message": None})
    ev = parse_event(line)
    assert ev is not None
    assert ev.kind == "unknown"


def test_non_dict_json_returns_none():
    assert parse_event("[1,2,3]") is None
    assert parse_event("42") is None
    assert parse_event("\"hi\"") is None
