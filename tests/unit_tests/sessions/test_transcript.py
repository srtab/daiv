from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.sites.models import Site
from django.urls import reverse
from django.utils import timezone

import pytest
from langchain_core.messages import AIMessage
from sessions.artifacts import serialize_artifact
from sessions.executor.lock import LiveRun, SessionActivity
from sessions.models import Run, RunArtifact, RunStatus, Session, SessionOrigin
from sessions.transcript import annotate_transcript, expired_run_turns, mark_retryable
from sessions.turns import build_turns

from core.constants import (
    CANCELLED_BY_COORDINATOR_MESSAGE,
    CANCELLED_BY_USER_MESSAGE,
    INTERRUPTED_MESSAGE,
    RUN_FAILED_MESSAGE,
)
from tests.unit_tests.sessions.conftest import make_artifact

CREATED_AT = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
FINISHED_AT = datetime(2026, 7, 31, 12, 5, 0, tzinfo=UTC)


def _run(
    rid,
    *,
    status=RunStatus.SUCCESSFUL,
    message_id="",
    error_message="",
    prompt="",
    trigger_type=SessionOrigin.CHAT,
    created_at=None,
    finished_at=None,
    is_cancelled=False,
):
    return SimpleNamespace(
        id=rid,
        status=status,
        message_id=message_id,
        error_message=error_message,
        prompt=prompt,
        trigger_type=trigger_type,
        created_at=created_at,
        finished_at=finished_at,
        is_cancelled=is_cancelled,
    )


def _holding(holder):
    return SessionActivity(
        in_flight=True, holder=holder, live=LiveRun(id="holder-1", stoppable=False, started_at=CREATED_AT)
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
    assert result[2] == {
        "id": "run-r2",
        "role": "user",
        "segments": [{"type": "text", "content": "please fix X"}],
        "run_id": "r2",
    }


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
    raw = "enqueue_failed: GitCommandError: clone failed\nTraceback (most recent call last): ..."
    runs = [_run("r1", trigger_type=SessionOrigin.SCHEDULE, status=RunStatus.FAILED, error_message=raw)]
    result = annotate_transcript(turns, runs)
    marker = result[-1]
    assert marker["role"] == "run_status"
    assert marker["status"] == "failed"
    assert marker["message"] == RUN_FAILED_MESSAGE
    assert "GitCommandError" not in marker["message"]
    assert "Traceback" not in marker["message"]


def test_a_leg_its_delegating_session_cancelled_renders_aborted_marker():
    turns = [_user("h1"), _assistant("a1")]
    traceback = "sessions.executor.run.RunStoppedError\nTraceback (most recent call last): ..."
    runs = [
        _run(
            "r1",
            trigger_type=SessionOrigin.DELEGATED_JOB,
            status=RunStatus.FAILED,
            error_message=traceback,
            is_cancelled=True,
        )
    ]
    assert annotate_transcript(turns, runs)[-1] == {
        "id": "run-status-r1",
        "role": "run_status",
        "status": "aborted",
        "message": CANCELLED_BY_COORDINATOR_MESSAGE,
    }


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
    no synthetic user turn. Tests the ``if not run.prompt`` branch in ``_prompt_turns``."""
    runs = [_run("r1", message_id="missing-id", status=RunStatus.FAILED, error_message=RUN_FAILED_MESSAGE, prompt="")]
    result = annotate_transcript([], runs)
    assert len(result) == 1
    assert result[0] == {
        "id": "run-status-r1",
        "role": "run_status",
        "status": "failed",
        "message": RUN_FAILED_MESSAGE,
        "run_id": "r1",
    }


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


def test_slot_holder_not_checkpointed_yet_shows_its_prompt_without_a_marker():
    holder = _run("r1", status=RunStatus.RUNNING, prompt="please fix X", trigger_type=SessionOrigin.UI_JOB)
    assert annotate_transcript([], [holder], _holding(holder)) == [
        {"id": "run-r1", "role": "user", "segments": [{"type": "text", "content": "please fix X"}], "run_id": "r1"}
    ]


@pytest.mark.parametrize("status", [RunStatus.READY, RunStatus.QUEUED, RunStatus.RUNNING])
def test_run_not_started_shows_its_prompt_as_pending(status):
    run = _run("r1", status=status, prompt="please fix X", trigger_type=SessionOrigin.UI_JOB)
    assert annotate_transcript([], [run], SessionActivity(in_flight=True, waiting=(run,))) == [
        {
            "id": "run-r1",
            "role": "user",
            "segments": [{"type": "text", "content": "please fix X"}],
            "pending": True,
            "run_id": "r1",
        }
    ]


@pytest.mark.parametrize("status", [RunStatus.READY, RunStatus.QUEUED, RunStatus.RUNNING])
def test_unfinished_run_of_a_session_no_longer_in_flight_contributes_nothing(status):
    runs = [_run("r1", status=status, prompt="please fix X", trigger_type=SessionOrigin.UI_JOB)]
    assert annotate_transcript([], runs) == []


@pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.RUNNING])
def test_run_behind_the_holder_stays_hidden_and_owns_no_turn(status):
    behind = _run("r1", status=status, prompt="second ask", trigger_type=SessionOrigin.API_JOB)
    holder = _run("r2", status=RunStatus.RUNNING, trigger_type=SessionOrigin.MR_WEBHOOK, created_at=CREATED_AT)
    turns = [_user("h1"), _assistant("a1")]

    result = annotate_transcript(turns, [behind, holder], _holding(holder))

    assert [t["id"] for t in result] == ["h1", "a1"]
    assert result[0]["sent_at"] == CREATED_AT.isoformat()


def test_unmatched_message_id_logs_only_for_finished_runs(caplog):
    # A FAILED or RUNNING run reaching synthetic recovery is the designed path (turn not
    # checkpointed, or not yet) → silent. A finished run whose message_id matches nothing is
    # anomalous (it should own a checkpointed turn) → warn so a correlation bug is visible.
    turns = [_user("h1"), _assistant("a1")]
    failed = _run("rf", message_id="gone", status=RunStatus.FAILED, error_message="e", prompt="p")
    running = _run("rr", message_id="pending", status=RunStatus.RUNNING, prompt="p")
    with caplog.at_level(logging.WARNING, logger="daiv.sessions"):
        annotate_transcript(turns, [failed])
        annotate_transcript(turns, [running])
    assert not caplog.records

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="daiv.sessions"):
        result = annotate_transcript(turns, [_run("rs", message_id="gone", status=RunStatus.SUCCESSFUL)])
    assert [t["id"] for t in result] == ["h1", "a1"]  # finished run contributes nothing
    assert any("no matching" in r.message and "gone" in r.getMessage() for r in caplog.records)


# --- Run-level sent/received timestamp stamping ---------------------------------


def test_sent_and_received_stamped_on_run_boundaries():
    # A completed run brackets its exchange: sent_at (created_at) on the owning user
    # turn, received_at (finished_at) on the run's last turn — machine ISO-8601.
    turns = [_user("h1"), _assistant("a1")]
    runs = [_run("r1", message_id="h1", created_at=CREATED_AT, finished_at=FINISHED_AT)]
    result = annotate_transcript(turns, runs)
    assert result[0]["id"] == "h1"
    assert result[0]["sent_at"] == CREATED_AT.isoformat()
    assert "received_at" not in result[0]
    assert result[1]["id"] == "a1"
    assert result[1]["received_at"] == FINISHED_AT.isoformat()
    assert "sent_at" not in result[1]


def test_received_absent_when_finished_at_is_none():
    # In-flight run (created but not finished): sent_at shows, no received_at anywhere.
    turns = [_user("h1"), _assistant("a1")]
    runs = [_run("r1", message_id="h1", created_at=CREATED_AT, finished_at=None)]
    result = annotate_transcript(turns, runs)
    assert result[0]["sent_at"] == CREATED_AT.isoformat()
    assert all("received_at" not in t for t in result)


def test_single_received_for_multiple_assistant_turns():
    # One run producing several assistant turns gets exactly one received_at, on the
    # segment's last turn; intermediate turns get neither timestamp.
    turns = [_user("h1"), _assistant("a1"), _assistant("a2")]
    runs = [_run("r1", message_id="h1", created_at=CREATED_AT, finished_at=FINISHED_AT)]
    result = annotate_transcript(turns, runs)
    assert result[0]["sent_at"] == CREATED_AT.isoformat()
    assert "received_at" not in result[1]  # intermediate assistant turn
    assert "sent_at" not in result[1]
    assert result[2]["received_at"] == FINISHED_AT.isoformat()
    assert [t for t in result if t.get("received_at")] == [result[2]]


def test_ordinal_fallback_stamps_sent_at():
    # Background/legacy run (no message_id) matched by ordinal still carries sent_at on
    # its user turn and received_at on its last turn.
    turns = [_user("h1"), _assistant("a1")]
    runs = [_run("r1", created_at=CREATED_AT, finished_at=FINISHED_AT)]
    result = annotate_transcript(turns, runs)
    assert result[0]["sent_at"] == CREATED_AT.isoformat()
    assert result[1]["received_at"] == FINISHED_AT.isoformat()


def test_synthetic_recovered_user_turn_carries_sent_at():
    # A FAILED run recovered via _synthetic_turns (its message never checkpointed) still
    # carries sent_at on the recovered prompt turn; the status marker gets no received_at.
    runs = [
        _run(
            "r1",
            message_id="missing",
            status=RunStatus.FAILED,
            error_message="boom",
            prompt="please fix X",
            created_at=CREATED_AT,
            finished_at=FINISHED_AT,
        )
    ]
    result = annotate_transcript([], runs)
    assert [t["id"] for t in result] == ["run-r1", "run-status-r1"]
    assert result[0]["sent_at"] == CREATED_AT.isoformat()
    assert "received_at" not in result[1]  # the run_status marker is not a real turn


def test_stamping_is_noop_without_run_timestamps():
    # Guard: runs lacking created_at/finished_at (the default helper shape used by the
    # pre-existing tests) add no new keys, so the old exact-turn assertions still hold.
    turns = [_user("h1"), _assistant("a1")]
    result = annotate_transcript(turns, [_run("r1", message_id="h1")])
    assert result == [{**_user("h1"), "run_id": "r1"}, _assistant("a1")]
    assert all("sent_at" not in t and "received_at" not in t for t in result)


def test_sent_at_not_stamped_on_assistant_first_head_segment():
    # A leading non-user turn buckets into the anonymous head segment (user_id=None),
    # reachable only by ordinal fallback. The role guard must keep sent_at off its
    # assistant turn; a regression dropping the guard would stamp sent_at on an AI turn.
    turns = [_assistant("a0"), _user("h1"), _assistant("a1")]
    runs = [_run("r1", created_at=CREATED_AT, finished_at=FINISHED_AT)]
    result = annotate_transcript(turns, runs)
    assert [t["id"] for t in result] == ["a0", "h1", "a1"]
    assert "sent_at" not in result[0]  # assistant-first head turn: guard declines sent_at
    assert result[0]["received_at"] == FINISHED_AT.isoformat()
    assert all("sent_at" not in t for t in result)


@pytest.mark.parametrize("trigger_type", [SessionOrigin.DELEGATION_RESUME, SessionOrigin.PIPELINE_WEBHOOK])
def test_a_prompt_daiv_wrote_is_marked_as_daivs(trigger_type):
    turns = [_user("u1"), _assistant("a1"), _user("u2", "The sub-jobs you delegated have finished"), _assistant("a2")]
    runs = [_run("r1", message_id="u1"), _run("r2", trigger_type=trigger_type)]

    result = annotate_transcript(turns, runs)

    assert (result[2]["author"], result[2]["origin"]) == ("daiv", trigger_type)
    assert "author" not in result[0]
    assert "author" not in result[3]


@pytest.mark.parametrize("trigger_type", [SessionOrigin.CHAT, SessionOrigin.SCHEDULE, SessionOrigin.MR_WEBHOOK])
def test_a_prompt_a_person_wrote_is_not_marked(trigger_type):
    result = annotate_transcript([_user("u1"), _assistant("a1")], [_run("r1", trigger_type=trigger_type)])

    assert "author" not in result[0]


def test_a_recovered_prompt_daiv_wrote_is_marked_as_daivs():
    run = _run(
        "r1", status=RunStatus.FAILED, prompt="The sub-jobs you delegated", trigger_type=SessionOrigin.DELEGATION_RESUME
    )

    result = annotate_transcript([], [run])

    assert (result[0]["role"], result[0]["author"]) == ("user", "daiv")


def test_each_run_anchors_its_first_turn():
    turns = [_user("h1"), _assistant("a1"), _user("h2"), _assistant("a2"), _assistant("a3")]
    runs = [_run("r1", message_id="h1"), _run("r2", message_id="h2")]

    result = annotate_transcript(turns, runs)

    assert [(t["id"], t.get("run_id")) for t in result] == [
        ("h1", "r1"),
        ("a1", None),
        ("h2", "r2"),
        ("a2", None),
        ("a3", None),
    ]


def test_ordinal_matched_run_anchors_its_first_turn():
    result = annotate_transcript([_user("h1"), _assistant("a1")], [_run("r1")])

    assert result[0]["run_id"] == "r1"


def test_recovered_failed_run_anchors_its_prompt_not_its_marker():
    run = _run("r1", status=RunStatus.FAILED, message_id="lost", prompt="do it")

    prompt, marker = annotate_transcript([], [run])

    assert (prompt["run_id"], "run_id" in marker) == ("r1", False)


def _db_run(session: Session, **kwargs) -> Run:
    fields = {"trigger_type": SessionOrigin.UI_JOB, "repo_id": session.repo_id, "status": RunStatus.SUCCESSFUL}
    return Run.objects.create(session=session, **{**fields, **kwargs})


def _db_session() -> Session:
    return Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="group/repo")


@pytest.mark.django_db
def test_expired_run_turns_synthesizes_a_publish_turn_per_run_with_artifacts():
    session = _db_session()
    first, empty, last = _db_run(session), _db_run(session), _db_run(session)
    report = make_artifact(first, filename="report.md", title="Findings")
    chart = make_artifact(first, filename="chart.png")
    page = make_artifact(last, filename="audit.html", title="Audit")

    result = expired_run_turns([first, empty, last], [])

    def segment(artifact):
        return {
            "type": "tool_call",
            "id": f"artifact-{artifact.id}",
            "name": "publish_artifact",
            "args": "",
            "result": json.dumps({"status": "published", **serialize_artifact(artifact).model_dump()}),
            "status": "done",
        }

    assert result == [
        {
            "id": f"run-{first.id}-artifacts",
            "role": "assistant",
            "segments": [segment(report), segment(chart)],
            "run_id": str(first.id),
        },
        {"id": f"run-{last.id}-artifacts", "role": "assistant", "segments": [segment(page)], "run_id": str(last.id)},
    ]


@pytest.mark.django_db
def test_expired_run_turns_artifact_segments_carry_the_build_turns_tool_call_keys():
    run = _db_run(_db_session())
    make_artifact(run)
    built = build_turns([AIMessage(content="", tool_calls=[{"id": "t1", "name": "publish_artifact", "args": {}}])])

    (turn,) = expired_run_turns([run], [])

    assert turn.keys() - {"run_id"} == built[0].keys()
    assert turn["segments"][0].keys() == built[0]["segments"][0].keys()


@pytest.mark.django_db
def test_expired_run_turns_mark_a_revised_artifact_updated():
    run = _db_run(_db_session())
    artifact = make_artifact(run)
    RunArtifact.objects.filter(pk=artifact.pk).update(updated_at=timezone.now())

    (turn,) = expired_run_turns([run], [])

    assert json.loads(turn["segments"][0]["result"])["status"] == "updated"


@pytest.mark.django_db
def test_expired_run_turns_leave_the_anchor_to_a_run_already_shown():
    session = _db_session()
    failed, published = _db_run(session), _db_run(session)
    make_artifact(failed)
    make_artifact(published)
    shown = [{"id": f"run-{failed.id}", "role": "user", "segments": [], "run_id": str(failed.id)}]

    result = expired_run_turns([failed, published], shown)

    assert [turn.get("run_id") for turn in result] == [None, str(published.id)]


@pytest.mark.django_db
def test_expired_run_turns_restore_a_finished_runs_prompt_ahead_of_its_artifacts():
    run = _db_run(_db_session(), prompt="audit it", finished_at=timezone.now())
    make_artifact(run)

    prompt, artifacts = expired_run_turns([run], [])

    assert (prompt["id"], prompt["run_id"], prompt["segments"][0]["content"]) == (
        f"run-{run.id}",
        str(run.id),
        "audit it",
    )
    assert ("run_id" in artifacts, artifacts["received_at"]) == (False, run.finished_at.isoformat())


@pytest.mark.django_db
def test_expired_run_turns_restore_no_prompt_for_a_run_already_shown():
    run = _db_run(_db_session(), prompt="go")
    shown = [{"id": f"run-{run.id}", "role": "user", "segments": [], "run_id": str(run.id)}]

    assert expired_run_turns([run], shown) == []


@pytest.mark.django_db
def test_expired_run_turns_restore_no_prompt_for_an_unfinished_run():
    run = _db_run(_db_session(), prompt="go", status=RunStatus.QUEUED)

    assert expired_run_turns([run], []) == []


@pytest.mark.django_db
def test_expired_run_turns_query_once_for_every_run(django_assert_num_queries):
    session = _db_session()
    runs = [_db_run(session) for _ in range(3)]
    for run in runs:
        make_artifact(run, filename="a.md")
        make_artifact(run, filename="b.md")
    Site.objects.get_current()
    runs = list(Run.objects.filter(pk__in=[r.pk for r in runs]).order_by("created_at"))

    with django_assert_num_queries(1):
        assert len(expired_run_turns(runs, [])) == 3


def _retryable(rid, repo_id="group/repo", *, is_retryable=True):
    return SimpleNamespace(id=rid, repo_id=repo_id, is_retryable=is_retryable)


def test_mark_retryable_links_the_run_composer_from_the_runs_anchor_turn():
    turns = [{**_user("h1"), "run_id": "r1"}, _assistant("a1")]

    with patch("sessions.transcript.runnable_repo_ids", return_value={"group/repo"}):
        urls = mark_retryable(turns, [_retryable("r1")], user=None)

    assert [t.get("retry_url") for t in turns] == [f"{reverse('runs:agent_run_new')}?from=r1", None]
    assert urls == {"r1": f"{reverse('runs:agent_run_new')}?from=r1"}


def test_mark_retryable_skips_a_run_the_viewer_cannot_run_on_the_repo():
    turns = [{**_user("h1"), "run_id": "r1"}]

    with patch("sessions.transcript.runnable_repo_ids", return_value=set()) as runnable:
        mark_retryable(turns, [_retryable("r1")], user="viewer")

    assert "retry_url" not in turns[0]
    runnable.assert_called_once_with("viewer", {"group/repo"})


def test_mark_retryable_skips_a_run_that_is_not_retryable_without_checking_access():
    turns = [{**_user("h1"), "run_id": "r1"}]

    with patch("sessions.transcript.runnable_repo_ids") as runnable:
        mark_retryable(turns, [_retryable("r1", is_retryable=False)], user=None)

    assert "retry_url" not in turns[0]
    runnable.assert_not_called()


def test_mark_retryable_checks_every_repo_in_one_call():
    turns = [{**_user(f"h{i}"), "run_id": f"r{i}"} for i in range(3)]
    runs = [_retryable("r0"), _retryable("r1"), _retryable("r2", repo_id="group/other")]

    with patch("sessions.transcript.runnable_repo_ids", return_value={"group/repo"}) as runnable:
        mark_retryable(turns, runs, user=None)

    assert [("retry_url" in t) for t in turns] == [True, True, False]
    runnable.assert_called_once_with(None, {"group/repo", "group/other"})
