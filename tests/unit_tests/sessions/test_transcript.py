from __future__ import annotations

from types import SimpleNamespace

from sessions.models import RunStatus, SessionOrigin
from sessions.transcript import annotate_transcript

from core.constants import CANCELLED_BY_USER_MESSAGE, RUN_FAILED_MESSAGE


def _run(
    rid, *, status=RunStatus.SUCCESSFUL, message_id="", error_message="", prompt="", trigger_type=SessionOrigin.CHAT
):
    return SimpleNamespace(
        id=rid,
        status=status,
        message_id=message_id,
        error_message=error_message,
        prompt=prompt,
        trigger_type=trigger_type,
    )


def _user(tid, text="hi"):
    return {"id": tid, "role": "user", "segments": [{"type": "text", "content": text}]}


def _assistant(tid, text="ok"):
    return {"id": tid, "role": "assistant", "segments": [{"type": "text", "content": text}]}


def test_successful_runs_add_no_markers():
    turns = [_user("h1"), _assistant("a1")]
    runs = [_run("r1", message_id="h1")]
    assert annotate_transcript(turns, runs) == turns


def test_failed_middle_run_inserts_marker_after_its_segment_by_message_id():
    turns = [_user("h1"), _assistant("a1"), _user("h2"), _assistant("a2")]
    runs = [
        _run(
            "r1", message_id="h1", status=RunStatus.FAILED, error_message="Run failed. Check server logs for details."
        ),
        _run("r2", message_id="h2"),
    ]
    result = annotate_transcript(turns, runs)
    assert [t["id"] for t in result] == ["h1", "a1", "run-status-r1", "h2", "a2"]
    marker = result[2]
    assert marker["role"] == "run_status"
    assert marker["status"] == "failed"
    assert marker["message"] == "Run failed. Check server logs for details."


def test_cancelled_run_renders_aborted_marker():
    turns = [_user("h1"), _assistant("a1")]
    runs = [_run("r1", message_id="h1", status=RunStatus.FAILED, error_message=CANCELLED_BY_USER_MESSAGE)]
    result = annotate_transcript(turns, runs)
    assert result[-1] == {
        "id": "run-status-r1",
        "role": "run_status",
        "status": "aborted",
        "message": CANCELLED_BY_USER_MESSAGE,
    }


def test_ordinal_fallback_when_message_id_missing():
    turns = [_user("h1"), _assistant("a1"), _user("h2"), _assistant("a2")]
    runs = [
        _run("r1"),  # background/legacy, no message_id -> ordinal (1st user turn)
        _run("r2", status=RunStatus.FAILED, error_message="boom"),  # ordinal (2nd user turn)
    ]
    result = annotate_transcript(turns, runs)
    assert [t["id"] for t in result] == ["h1", "a1", "h2", "a2", "run-status-r2"]


def test_pre_checkpoint_failure_synthesizes_user_turn_and_marker():
    # Run failed before its human message reached the checkpoint: message_id set but absent from turns.
    turns = [_user("h1"), _assistant("a1")]
    runs = [
        _run("r1", message_id="h1"),
        _run("r2", message_id="h2-missing", status=RunStatus.FAILED, error_message="boom", prompt="please fix X"),
    ]
    result = annotate_transcript(turns, runs)
    assert [t["id"] for t in result] == ["h1", "a1", "run-r2", "run-status-r2"]
    assert result[2] == {"id": "run-r2", "role": "user", "segments": [{"type": "text", "content": "please fix X"}]}


def test_multiple_failed_middle_runs_each_get_a_marker():
    turns = [_user("h1"), _assistant("a1"), _user("h2"), _assistant("a2"), _user("h3"), _assistant("a3")]
    runs = [
        _run("r1", message_id="h1", status=RunStatus.FAILED, error_message="e1"),
        _run("r2", message_id="h2", status=RunStatus.FAILED, error_message="e2"),
        _run("r3", message_id="h3"),
    ]
    result = annotate_transcript(turns, runs)
    assert [t["id"] for t in result] == ["h1", "a1", "run-status-r1", "h2", "a2", "run-status-r2", "h3", "a3"]


def test_expired_session_with_only_successful_runs_yields_no_turns():
    # No checkpoint (turns empty), old successful runs -> nothing to render (expired banner handles it).
    assert annotate_transcript([], [_run("r1", message_id="h1")]) == []


def test_non_chat_failed_run_shows_generic_message_not_raw_error():
    turns = [_user("h1"), _assistant("a1")]
    raw = "dispatch_failed: GitCommandError: clone failed\nTraceback (most recent call last): ..."
    runs = [_run("r1", trigger_type=SessionOrigin.SCHEDULE, status=RunStatus.FAILED, error_message=raw)]
    result = annotate_transcript(turns, runs)
    marker = result[-1]
    assert marker["role"] == "run_status"
    assert marker["status"] == "failed"
    assert marker["message"] == RUN_FAILED_MESSAGE
    assert "GitCommandError" not in marker["message"]
    assert "Traceback" not in marker["message"]


def test_chat_failed_run_renders_error_message_verbatim():
    # Chat runs use the streamer's fixed, user-safe constants — render verbatim.
    turns = [_user("h1"), _assistant("a1")]
    runs = [_run("r1", trigger_type=SessionOrigin.CHAT, status=RunStatus.FAILED, error_message="some chat message")]
    result = annotate_transcript(turns, runs)
    marker = result[-1]
    assert marker["role"] == "run_status"
    assert marker["message"] == "some chat message"


def test_chat_failed_run_with_empty_error_message_falls_back_to_generic():
    """CHAT run that FAILED with error_message="" → transcript.py:26 fallback to RUN_FAILED_MESSAGE.
    Guards the ``run.error_message or RUN_FAILED_MESSAGE`` branch in ``_marker``."""
    turns = [_user("h1"), _assistant("a1")]
    runs = [_run("r1", trigger_type=SessionOrigin.CHAT, status=RunStatus.FAILED, error_message="")]
    result = annotate_transcript(turns, runs)
    marker = result[-1]
    assert marker["role"] == "run_status"
    assert marker["status"] == "failed"
    assert marker["message"] == RUN_FAILED_MESSAGE


def test_synthetic_turns_marker_only_when_no_prompt():
    """_synthetic_turns with prompt="" and unmatched message_id → result is exactly [marker],
    no synthetic user turn. Tests the ``if run.prompt`` branch in ``_synthetic_turns``."""
    runs = [_run("r1", message_id="missing-id", status=RunStatus.FAILED, error_message=RUN_FAILED_MESSAGE, prompt="")]
    result = annotate_transcript([], runs)
    assert len(result) == 1
    assert result[0] == {"id": "run-status-r1", "role": "run_status", "status": "failed", "message": RUN_FAILED_MESSAGE}
