"""Tests for Run.sync_from_task_result / sync_and_save usage-parsing branches.

These paths survive verbatim from Activity but were no longer exercised: the
input_tokens re-sync guard, the invalid-cost fallback, the no-usage case, and the
sync_and_save no-op short-circuit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

pytestmark = pytest.mark.django_db

_STARTED = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)
_FINISHED = datetime(2026, 5, 1, 10, 5, 0, tzinfo=UTC)


def _session() -> Session:
    return Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="g/r")


def _run(task_result, **kwargs) -> Run:
    defaults = {
        "session": _session(),
        "trigger_type": SessionOrigin.UI_JOB,
        "repo_id": "g/r",
        "status": RunStatus.RUNNING,
        "task_result": task_result,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


def _usage(**over):
    usage = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_usd": "0.12", "by_model": {"m": 1}}
    usage.update(over)
    return usage


def test_usage_parsed_into_run(create_db_task_result):
    tr = create_db_task_result(
        status="SUCCESSFUL",
        return_value={"response": "Done.", "usage": _usage()},
        started_at=_STARTED,
        finished_at=_FINISHED,
    )
    run = _run(tr)
    assert run.sync_and_save() is True
    run.refresh_from_db()
    assert run.status == RunStatus.SUCCESSFUL
    assert (run.input_tokens, run.output_tokens, run.total_tokens) == (100, 50, 150)
    assert run.cost_usd == Decimal("0.12")
    assert run.usage_by_model == {"m": 1}


def test_invalid_cost_usd_dropped_but_tokens_synced(create_db_task_result):
    tr = create_db_task_result(
        status="SUCCESSFUL", return_value={"response": "x", "usage": _usage(cost_usd="not-a-number")}
    )
    run = _run(tr)
    with patch("sessions.models.logger") as m_log:
        run.sync_from_task_result()
    assert run.input_tokens == 100  # tokens still synced
    assert run.cost_usd is None  # bad cost dropped
    m_log.warning.assert_called_once()


def test_usage_not_resynced_when_input_tokens_already_set(create_db_task_result):
    tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "x", "usage": _usage(input_tokens=999)})
    run = _run(tr, input_tokens=7)
    run.sync_from_task_result()
    assert run.input_tokens == 7  # the `input_tokens is None` guard prevents overwrite


def test_no_usage_leaves_token_fields_null(create_db_task_result):
    tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "x"})
    run = _run(tr)
    run.sync_from_task_result()
    assert run.input_tokens is None
    assert run.total_tokens is None
    assert run.cost_usd is None


def test_sync_and_save_returns_false_when_nothing_changed(create_db_task_result):
    tr = create_db_task_result(
        status="SUCCESSFUL", return_value={"response": "Done."}, started_at=_STARTED, finished_at=_FINISHED
    )
    run = _run(
        tr,
        status=RunStatus.SUCCESSFUL,
        started_at=_STARTED,
        finished_at=_FINISHED,
        result_summary="Done.",
        input_tokens=1,
    )
    assert run.sync_and_save() is False


def test_sync_and_save_finishes_a_terminal_transition(create_db_task_result):
    tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "Done."})
    run = _run(tr, status=RunStatus.RUNNING)
    with patch.object(Run.objects, "finish", wraps=Run.objects.finish) as finish:
        assert run.sync_and_save() is True
    finish.assert_called_once()
    assert finish.call_args.args[1] == RunStatus.SUCCESSFUL
    run.refresh_from_db()
    assert (run.status, run.result_summary) == (RunStatus.SUCCESSFUL, "Done.")


def test_sync_and_save_does_not_refinish_a_row_another_sync_finished(create_db_task_result, run_finished_spy):
    """Two syncs of one task (the worker's ``task_finished`` and the ``sync_stuck_runs`` cron) both read the row
    RUNNING; only the first may finish it, or ``run_finished`` reaches the receivers twice."""
    tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "Done."})
    first = _run(tr, status=RunStatus.RUNNING)
    second = Run.objects.select_related("task_result").get(pk=first.pk)
    assert first.sync_and_save() is True
    assert second.sync_and_save() is False
    assert run_finished_spy.call_count == 1


def test_a_stale_sync_does_not_reopen_a_finished_row(create_db_task_result):
    """``sync_stuck_runs`` read the row READY while its task ran; the worker finished the row before the cron's
    write, which must not move it back to RUNNING, or the next sync finishes it and emits a second time."""
    tr = create_db_task_result(status="RUNNING", started_at=_STARTED)
    stale = Run.objects.select_related("task_result").get(pk=_run(tr, status=RunStatus.READY).pk)
    Run.objects.filter(pk=stale.pk).update(status=RunStatus.SUCCESSFUL)

    assert stale.sync_and_save() is False

    stale.refresh_from_db()
    assert (stale.status, stale.started_at) == (RunStatus.SUCCESSFUL, None)


def test_a_sync_that_starts_the_run_refreshes_the_badge_once_committed(
    create_db_task_result, badge, django_capture_on_commit_callbacks
):
    tr = create_db_task_result(status="RUNNING", started_at=_STARTED)
    run = _run(tr, status=RunStatus.READY)

    with django_capture_on_commit_callbacks(execute=True):
        assert run.sync_and_save() is True

    badge.assert_called_once_with()
    run.refresh_from_db()
    assert (run.status, run.started_at) == (RunStatus.RUNNING, _STARTED)
