from __future__ import annotations

import logging
from types import SimpleNamespace

from sessions.models import RunStatus, SessionOrigin
from sessions.transcript import annotate_transcript

from core.constants import CANCELLED_BY_USER_MESSAGE, INTERRUPTED_MESSAGE, RUN_FAILED_MESSAGE


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


def test_interrupted_run_renders_aborted_marker():
    # A stale-takeover / process shutdown records INTERRUPTED_MESSAGE. It is a neutral,
    # non-failure termination and must render as the grey "aborted" marker (not the red
    # failure chip), same class as an explicit user cancel.
    turns = [_user("h1"), _assistant("a1")]
    runs = [_run("r1", message_id="h1", status=RunStatus.FAILED, error_message=INTERRUPTED_MESSAGE)]
    result = annotate_transcript(turns, runs)
    assert result[-1] == {
        "id": "run-status-r1",
        "role": "run_status",
        "status": "aborted",
        "message": INTERRUPTED_MESSAGE,
    }


def test_message_id_match_flushes_intervening_unowned_segment():
    # A run whose message_id matches a segment *past* the cursor forces the intervening,
    # run-less segment to flush first so ordering is preserved. Covers the
    # ``while cursor < matched`` skip-flush branch (only one run, matching the 2nd turn).
    turns = [_user("h1"), _assistant("a1"), _user("h2"), _assistant("a2")]
    runs = [_run("r2", message_id="h2", status=RunStatus.FAILED, error_message=RUN_FAILED_MESSAGE)]
    result = annotate_transcript(turns, runs)
    assert [t["id"] for t in result] == ["h1", "a1", "h2", "a2", "run-status-r2"]


def test_trailing_segments_without_runs_are_flushed():
    # Fewer runs than user turns (e.g. a pruned/legacy run row): the turns no run owns must
    # still render. Covers the final ``while cursor < len(segments)`` trailing flush.
    turns = [_user("h1"), _assistant("a1"), _user("h2"), _assistant("a2")]
    runs = [_run("r1", message_id="h1", status=RunStatus.FAILED, error_message="e1")]
    result = annotate_transcript(turns, runs)
    assert [t["id"] for t in result] == ["h1", "a1", "run-status-r1", "h2", "a2"]


def test_leading_non_user_turn_buckets_into_head_segment():
    # A transcript starting with a non-user turn (rare) buckets into an anonymous head
    # segment (user_id=None) that no message_id can match; it is flushed positionally so it
    # is never dropped, and a later run still matches its own user turn.
    turns = [_assistant("a0"), _user("h1"), _assistant("a1")]
    runs = [_run("r1", message_id="h1", status=RunStatus.FAILED, error_message="e1")]
    result = annotate_transcript(turns, runs)
    assert [t["id"] for t in result] == ["a0", "h1", "a1", "run-status-r1"]


def test_empty_transcript_recovers_every_failed_run():
    # Expired/lapsed checkpoint (no turns): each FAILED run is recovered as prompt + marker,
    # so the view renders the history instead of a bare expired banner.
    runs = [
        _run("r1", message_id="h1", status=RunStatus.FAILED, error_message="e1", prompt="first ask"),
        _run("r2", message_id="h2", status=RunStatus.FAILED, error_message="e2", prompt="second ask"),
    ]
    result = annotate_transcript([], runs)
    assert [t["id"] for t in result] == ["run-r1", "run-status-r1", "run-r2", "run-status-r2"]
    assert result[0]["segments"][0]["content"] == "first ask"
    assert result[2]["segments"][0]["content"] == "second ask"


def test_unmatched_message_id_logs_only_for_non_failed_runs(caplog):
    # A FAILED run reaching synthetic recovery is the designed path (pre-checkpoint failure)
    # → silent, no log noise. A non-failed run whose message_id matches nothing is anomalous
    # (a successful run should own a checkpointed turn) → warn so a correlation bug is visible.
    turns = [_user("h1"), _assistant("a1")]
    failed = _run("rf", message_id="gone", status=RunStatus.FAILED, error_message="e", prompt="p")
    with caplog.at_level(logging.WARNING, logger="daiv.sessions"):
        annotate_transcript(turns, [failed])
    assert not caplog.records

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="daiv.sessions"):
        result = annotate_transcript(turns, [_run("rs", message_id="gone", status=RunStatus.SUCCESSFUL)])
    assert [t["id"] for t in result] == ["h1", "a1"]  # non-failed run contributes nothing
    assert any("no matching" in r.message and "gone" in r.getMessage() for r in caplog.records)
