"""Turn and segment builders shaped like ``build_turns`` / ``annotate_transcript`` output, for the JS suites."""

from __future__ import annotations

import json
from typing import Any

from automation.agent.questions import QUESTION_DELIVERED
from core.constants import CANCELLED_BY_USER_MESSAGE, RUN_FAILED_MESSAGE

SENT = "2026-10-29T10:00:00+00:00"
STARTED = "2026-10-29T10:00:01+00:00"
ENDED = "2026-10-29T10:03:13+00:00"


def user(turn_id: str, *, started_at: str | None = STARTED, pending: bool = False) -> dict[str, Any]:
    turn = {"id": turn_id, "role": "user", "segments": [text("go")], "run_id": f"run-{turn_id}", "sent_at": SENT}
    if started_at is not None:
        turn["started_at"] = started_at
    if pending:
        turn["pending"] = True
    return turn


def asst(turn_id: str, *segs: dict[str, Any], streaming: bool = False, received_at: str | None = ENDED) -> dict:
    turn = {"id": turn_id, "role": "assistant", "segments": list(segs)}
    if streaming:
        turn["streaming"] = True
    if received_at is not None:
        turn["received_at"] = received_at
    return turn


def text(s: str) -> dict[str, Any]:
    return {"type": "text", "content": s}


def thinking(status: str = "done") -> dict[str, Any]:
    seg = {"type": "thinking", "content": "Considering the options."}
    if status != "done":
        seg["status"] = status
    return seg


def tool_call(call_id: str, name: str, args: dict, *, result: str | None, status: str = "done") -> dict[str, Any]:
    args_str = json.dumps(args)
    return {"type": "tool_call", "id": call_id, "name": name, "args": args_str, "result": result, "status": status}


def read(call_id: str, *, path: str = "/workspace/repo/x", result: str | None = "1  x", status: str = "done") -> dict:
    return tool_call(call_id, "read_file", {"file_path": path}, result=result, status=status)


def grep(call_id: str, *, status: str = "done") -> dict[str, Any]:
    return tool_call(call_id, "grep", {"pattern": "foo"}, result="x.py" if status == "done" else None, status=status)


def bash(call_id: str, *, command: str = "pytest", exit_code: int = 0, status: str = "done") -> dict[str, Any]:
    outcome = {"commands": [{"command": command, "exit_code": exit_code, "output": ""}], "files_changed": []}
    result = json.dumps(outcome) if status == "done" else None
    return tool_call(call_id, "bash", {"command": command}, result=result, status=status)


def edit(call_id: str, old: str, new: str) -> dict[str, Any]:
    args = {"file_path": "/workspace/repo/x.py", "old_string": old, "new_string": new}
    return tool_call(call_id, "edit_file", args, result="Successfully replaced 1 instance.")


def publish(call_id: str) -> dict[str, Any]:
    artifact_id = f"art-{call_id}"
    result = {
        "status": "published",
        "id": artifact_id,
        "title": "Report",
        "kind": "markdown",
        "size": 10,
        "url": f"/artifacts/{artifact_id}/",
    }
    return tool_call(call_id, "publish_artifact", {"path": "/workspace/tmp/report.md"}, result=json.dumps(result))


def delegate(call_id: str) -> dict[str, Any]:
    return tool_call(call_id, "delegate_jobs", {"jobs": []}, result='{"batch_id": "b1", "jobs": []}')


def refused_question(call_id: str) -> dict[str, Any]:
    return tool_call(call_id, "ask_user_question", {"questions": []}, result="error: ask alone", status="error")


def question(call_id: str) -> dict[str, Any]:
    args = {"questions": [{"header": "Approach", "question": "Which fix?", "options": [], "multi_select": False}]}
    return tool_call(call_id, "ask_user_question", args, result=QUESTION_DELIVERED)


def status_turn(status: str) -> dict[str, Any]:
    message = CANCELLED_BY_USER_MESSAGE if status == "aborted" else RUN_FAILED_MESSAGE
    return {"id": f"run-status-{status}", "role": "run_status", "status": status, "message": message}
