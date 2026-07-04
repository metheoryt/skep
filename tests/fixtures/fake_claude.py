"""Test double for the `claude` binary: emits canned stream-json, then exits.

Usage mirrors the real invocation enough for AgentProcess: the prompt is passed
as the last positional arg; flags are ignored. Emits a system init, one
assistant text, one tool_use, and a result line.
"""
import json
import sys
import time


def main() -> None:
    prompt = sys.argv[-1]
    lines = [
        {"type": "system", "subtype": "init", "session_id": "fake-sess"},
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": f"working on: {prompt}"}]}},
        {"type": "assistant",
         "message": {"content": [{"type": "tool_use", "name": "edit_file", "input": {}}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "finished", "session_id": "fake-sess"},
    ]
    for obj in lines:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
        time.sleep(0.01)


if __name__ == "__main__":
    main()
