from __future__ import annotations

import json
from typing import Any

REGISTER = "register"
HEARTBEAT = "heartbeat"
TASK_STARTED = "task_started"
ACTIVITY = "activity"
MILESTONE = "milestone"
DONE = "done"
LS_REPLY = "ls_reply"
SPAWN_REJECTED = "spawn_rejected"
SPAWN = "spawn"
KILL = "kill"
PANIC = "panic"
LS_REQUEST = "ls_request"


def encode(msg: dict[str, Any]) -> str:
    return json.dumps(msg, separators=(",", ":"))


def decode(raw: str) -> dict[str, Any]:
    obj = json.loads(raw)
    if not isinstance(obj, dict) or "t" not in obj:
        raise ValueError(f"malformed message: {raw!r}")
    return obj


def register_msg(
    host: str, profile: str, version: str, active_tasks: list[dict[str, Any]]
) -> dict[str, Any]:
    """`active_tasks` entries: {"local_id", "repo", "title", "session_local_id"}.

    `session_local_id` (int | None) lets the queen's reconnect replay reuse a
    known session's ref/topic instead of minting a fresh one; workers built
    before this field simply omit the key and replay tolerates that (Sessions
    A2 task 3).
    """
    return {
        "t": REGISTER,
        "host": host,
        "profile": profile,
        "version": version,
        "active_tasks": active_tasks,
    }


def heartbeat_msg(
    active_tasks: list[dict[str, Any]], capacity_remaining: int
) -> dict[str, Any]:
    """`active_tasks` entries carry the same shape as `register_msg`'s, including
    `session_local_id`."""
    return {
        "t": HEARTBEAT,
        "active_tasks": active_tasks,
        "capacity_remaining": capacity_remaining,
    }


def task_started_msg(
    local_id: int, repo: str, title: str, session_local_id: int | None = None
) -> dict[str, Any]:
    return {
        "t": TASK_STARTED,
        "local_id": local_id,
        "repo": repo,
        "title": title,
        "session_local_id": session_local_id,
    }


def activity_msg(local_id: int, line: str) -> dict[str, Any]:
    return {"t": ACTIVITY, "local_id": local_id, "line": line}


def milestone_msg(local_id: int, text: str) -> dict[str, Any]:
    return {"t": MILESTONE, "local_id": local_id, "text": text}


def done_msg(local_id: int, status: str, summary: str) -> dict[str, Any]:
    return {"t": DONE, "local_id": local_id, "status": status, "summary": summary}


def spawn_rejected_msg(reason: str) -> dict[str, Any]:
    return {"t": SPAWN_REJECTED, "reason": reason}


def spawn_msg(
    repo: str, task: str, roots: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    # `roots` carries names only -- never paths (spec section 4).
    return {"t": SPAWN, "repo": repo, "task": task, "roots": roots}


def kill_msg(task_id: int) -> dict[str, Any]:
    return {"t": KILL, "task_id": task_id}


def panic_msg() -> dict[str, Any]:
    return {"t": PANIC}


def ls_request_msg() -> dict[str, Any]:
    return {"t": LS_REQUEST}


MAILBOX_SEND = "mailbox_send"
MAILBOX_ACK = "mailbox_ack"
INBOX_READ = "inbox_read"
INBOX_REPLY = "inbox_reply"


def mailbox_send_msg(
    req_id: str,
    tid: int,
    to: str,
    subject: str,
    body: str,
    in_reply_to: int | None,
) -> dict[str, Any]:
    return {
        "t": MAILBOX_SEND,
        "req_id": req_id,
        "tid": tid,
        "to": to,
        "subject": subject,
        "body": body,
        "in_reply_to": in_reply_to,
    }


def mailbox_ack_msg(
    req_id: str,
    ok: bool,
    message_id: int | None,
    error: str | None,
    status: str,
) -> dict[str, Any]:
    return {
        "t": MAILBOX_ACK,
        "req_id": req_id,
        "ok": ok,
        "message_id": message_id,
        "error": error,
        "status": status,
    }


def inbox_read_msg(req_id: str, tid: int) -> dict[str, Any]:
    return {"t": INBOX_READ, "req_id": req_id, "tid": tid}


def inbox_reply_msg(req_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"t": INBOX_REPLY, "req_id": req_id, "messages": messages}
