import uuid
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

import pytest
from django_tasks.base import DEFAULT_TASK_PRIORITY
from django_tasks_db.models import DBTaskResult
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin
from sessions.tasks import (
    RECLASSIFY_GRACE,
    RECLASSIFY_MAX_AGE,
    generate_batch_title_task,
    generate_title_task,
    reclassify_missing_envelopes_cron_task,
    sync_stuck_runs_cron_task,
)

from automation.titling.llm import TitlerNotConfiguredError
from core.constants import TASK_QUEUE_INTERACTIVE


def test_sync_stuck_runs_cron_task_dispatches_command():
    """The cron task dispatches the sync_stuck_runs management command.

    Guards the wiring (command name + the ``@locked_task`` decorator that ``.func()``
    exercises), not crontask/django_tasks framework behavior.
    """
    with patch("sessions.tasks.call_command") as mock_call_command:
        sync_stuck_runs_cron_task.func()

    mock_call_command.assert_called_once_with("sync_stuck_runs")


# --- reclassify_missing_envelopes_cron_task (Epic 1 review backstop) --------


def _stranded_run(
    *, status=RunStatus.SUCCESSFUL, trigger_type=SessionOrigin.SCHEDULE, finished_age=None, classify_eligible=True
) -> Run:
    """A terminal Run whose finished_at is pushed past the grace window (and inside the max-age floor)."""
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="group/repo")
    run = Run.objects.create(
        session=session,
        trigger_type=trigger_type,
        repo_id="group/repo",
        status=status,
        classify_eligible=classify_eligible,
    )
    finished = timezone.now() - (finished_age if finished_age is not None else RECLASSIFY_GRACE + timedelta(minutes=1))
    Run.objects.filter(pk=run.pk).update(finished_at=finished)
    run.refresh_from_db()
    return run


@pytest.mark.django_db
def test_reclassify_reenqueues_stranded_terminal_runs_across_origins():
    stranded = [
        _stranded_run(status=RunStatus.SUCCESSFUL, trigger_type=SessionOrigin.SCHEDULE),
        _stranded_run(status=RunStatus.FAILED, trigger_type=SessionOrigin.MR_WEBHOOK),
        _stranded_run(status=RunStatus.SUCCESSFUL, trigger_type=SessionOrigin.API_JOB),
    ]
    with patch("sessions.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    enqueued = {call.args[0] for call in task.enqueue.call_args_list}
    assert enqueued == {str(r.pk) for r in stranded}


@pytest.mark.django_db
def test_reclassify_skips_chat_classified_nonterminal_and_out_of_window():
    classified = _stranded_run()
    RunEnvelope.objects.create(run=classified, status=EnvelopeStatus.ALL_CLEAR)  # already has an envelope
    _stranded_run(status=RunStatus.RUNNING)  # non-terminal
    _stranded_run(trigger_type=SessionOrigin.CHAT)  # chat is never classified
    _stranded_run(finished_age=timedelta(minutes=1))  # inside the grace window
    _stranded_run(finished_age=RECLASSIFY_MAX_AGE + timedelta(hours=1))  # older than the recency floor
    with patch("sessions.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    task.enqueue.assert_not_called()


@pytest.mark.django_db
def test_reclassify_keys_on_finished_at_not_created_at():
    # created_at old but finished_at recent → still re-targeted (batch siblings can finish long after creation).
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="group/repo")
    run = Run.objects.create(
        session=session, trigger_type=SessionOrigin.SCHEDULE, repo_id="group/repo", status=RunStatus.SUCCESSFUL
    )
    Run.objects.filter(pk=run.pk).update(
        created_at=timezone.now() - (RECLASSIFY_MAX_AGE + timedelta(days=2)),
        finished_at=timezone.now() - (RECLASSIFY_GRACE + timedelta(minutes=1)),
    )
    with patch("sessions.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    assert {call.args[0] for call in task.enqueue.call_args_list} == {str(run.pk)}


@pytest.mark.django_db
def test_reclassify_skips_ineligible_runs():
    """Pre-deploy runs (classify_eligible=False) are out of scope; eligible siblings are still re-enqueued."""
    eligible = _stranded_run(classify_eligible=True)
    _stranded_run(classify_eligible=False)  # pre-deploy backlog: must be skipped
    with patch("sessions.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    assert {call.args[0] for call in task.enqueue.call_args_list} == {str(eligible.pk)}


@pytest.mark.django_db
def test_reclassify_reenqueues_stranded_waiting_input_runs():
    stranded = [_stranded_run(status=RunStatus.SUCCESSFUL), _stranded_run(status=RunStatus.WAITING_INPUT)]
    with patch("sessions.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    assert {call.args[0] for call in task.enqueue.call_args_list} == {str(r.pk) for r in stranded}


def _run_for_titling(
    *, title: str = "", session_title: str = "", batch_id: uuid.UUID | None = None, repo_id: str = "group/repo"
) -> Run:
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id=repo_id, title=session_title
    )
    return Run.objects.create(
        session=session, trigger_type=SessionOrigin.API_JOB, repo_id=repo_id, batch_id=batch_id, title=title
    )


@pytest.mark.django_db
def test_generate_title_task_skips_a_missing_entity_without_calling_the_model():
    with patch("automation.titling.llm.generate_title") as generate:
        generate_title_task.func(entity_type="run", pk=str(uuid.uuid4()), prompt="any", repo_id="x/y")

    generate.assert_not_called()


@pytest.mark.django_db
def test_generate_title_task_overwrites_the_run_title_with_the_generated_one():
    run = _run_for_titling(title="Heuristic placeholder")

    with patch("automation.titling.llm.generate_title", return_value="LLM generated") as generate:
        generate_title_task.func(
            entity_type="run", pk=str(run.pk), prompt="add login", repo_id="group/repo", ref="feat/x"
        )

    run.refresh_from_db()
    assert run.title == "LLM generated"
    generate.assert_called_once_with(
        "add login",
        repo_id="group/repo",
        ref="feat/x",
        run_metadata={"entity_type": "run", "entity_pk": str(run.pk), "repo_id": "group/repo", "ref": "feat/x"},
    )


@pytest.mark.django_db
def test_generate_title_task_writes_a_session_title():
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/repo")

    with patch("automation.titling.llm.generate_title", return_value="Session title"):
        generate_title_task.func(entity_type="session", pk=session.thread_id, prompt="do a thing", repo_id="group/repo")

    session.refresh_from_db()
    assert session.title == "Session title"


@pytest.mark.django_db
def test_generate_title_task_keeps_the_title_when_no_model_is_configured():
    run = _run_for_titling(title="Heuristic placeholder")

    with patch("automation.titling.llm.generate_title", side_effect=TitlerNotConfiguredError("no key")):
        generate_title_task.func(entity_type="run", pk=str(run.pk), prompt="any", repo_id="group/repo")

    run.refresh_from_db()
    assert run.title == "Heuristic placeholder"


@pytest.mark.django_db
def test_generate_title_task_lets_a_failed_model_call_fail_the_task():
    run = _run_for_titling()

    with (
        patch("automation.titling.llm.generate_title", side_effect=RuntimeError("provider down")),
        pytest.raises(RuntimeError, match="provider down"),
    ):
        generate_title_task.func(entity_type="run", pk=str(run.pk), prompt="any", repo_id="group/repo")


@pytest.mark.django_db
def test_generate_batch_title_task_titles_only_the_untitled_runs_of_the_batch():
    batch_id = uuid.uuid4()
    untitled = [_run_for_titling(batch_id=batch_id, repo_id=f"o/r{i}") for i in range(2)]
    prefilled = _run_for_titling(batch_id=batch_id, title="job · run #1", repo_id="o/sched")
    outsider = _run_for_titling(repo_id="o/other")

    with patch("automation.titling.llm.generate_title", return_value="Add login feature") as generate:
        generate_batch_title_task.func(batch_id=str(batch_id), prompt="add login")

    generate.assert_called_once_with("add login", run_metadata={"entity_type": "run_batch", "batch_id": str(batch_id)})
    titles = {run.pk: Run.objects.get(pk=run.pk).title for run in [*untitled, prefilled, outsider]}
    assert titles == {
        untitled[0].pk: "Add login feature",
        untitled[1].pk: "Add login feature",
        prefilled.pk: "job · run #1",
        outsider.pk: "",
    }


@pytest.mark.django_db
def test_generate_batch_title_task_backfills_only_untitled_sessions():
    batch_id = uuid.uuid4()
    untitled = _run_for_titling(batch_id=batch_id)
    pinned = _run_for_titling(batch_id=batch_id, session_title="Session pinned")

    with patch("automation.titling.llm.generate_title", return_value="Batch title"):
        generate_batch_title_task.func(batch_id=str(batch_id), prompt="task")

    assert Session.objects.get(pk=untitled.session_id).title == "Batch title"
    assert Session.objects.get(pk=pinned.session_id).title == "Session pinned"


@pytest.mark.django_db
def test_generate_batch_title_task_keeps_titles_when_no_model_is_configured():
    batch_id = uuid.uuid4()
    run = _run_for_titling(batch_id=batch_id)

    with patch("automation.titling.llm.generate_title", side_effect=TitlerNotConfiguredError("no key")):
        generate_batch_title_task.func(batch_id=str(batch_id), prompt="task")

    run.refresh_from_db()
    assert run.title == ""


@pytest.mark.django_db
def test_the_enqueued_title_row_is_what_the_interactive_worker_claims(database_task_backend):
    """DAIV's own backend overrides ``enqueue``, and the row is what a worker selects on."""
    result = generate_title_task.enqueue(entity_type="session", pk=str(uuid.uuid4()), prompt="add login", repo_id="o/r")

    row = DBTaskResult.objects.get(id=result.id)
    assert row.queue_name == TASK_QUEUE_INTERACTIVE
    assert row.priority > DEFAULT_TASK_PRIORITY
    assert row.task_path == "sessions.tasks.generate_title_task"
