from skep.formatting import activity_line, escape_md, milestone_message
from skep.stream import Event


def test_escape_md_escapes_reserved_chars():
    assert escape_md("a_b*c[d]") == r"a\_b\*c\[d\]"
    assert escape_md("v1.2-3") == r"v1\.2\-3"


def test_activity_line_for_assistant_text_is_plain():
    ev = Event(kind="assistant_text", text="Refactoring the module")
    assert activity_line(ev) == "💬 Refactoring the module"


def test_activity_line_for_tool_use_is_plain():
    ev = Event(kind="tool_use", tool_name="edit_file")
    assert activity_line(ev) == "🔧 edit_file"


def test_activity_line_none_for_tool_result():
    assert activity_line(Event(kind="tool_result")) is None


def test_milestone_for_successful_result_is_plain():
    ev = Event(kind="result", text="All done", is_error=False)
    assert milestone_message(ev) == "✅ Done: All done"


def test_milestone_for_error_result_is_plain():
    ev = Event(kind="result", text="boom", is_error=True)
    assert milestone_message(ev) == "❌ Failed: boom"


def test_milestone_none_for_assistant_text():
    assert milestone_message(Event(kind="assistant_text", text="x")) is None


def test_activity_line_truncates_long_text():
    ev = Event(kind="assistant_text", text="x" * 300)
    line = activity_line(ev)
    assert len(line) <= 200
    assert line.endswith("…")
