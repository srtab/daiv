"""Tests for ``sessions.tasks.classify_run_task`` (Story 1.3, AC2-AC6).

Async tests (``asyncio_mode = "auto"`` — no ``@pytest.mark.asyncio``). The classification
*method* is mocked at ``sessions.classification._build_structured_llm`` so no real model is
called; the deterministic gating/invariants (enforced in the task) are what these exercise.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from django.db import IntegrityError

import pytest
from asgiref.sync import sync_to_async
from sessions.classification import ActionableDraft, RunClassification
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin
from sessions.tasks import classify_run_task

from accounts.models import User
from schedules.models import Frequency, Intent, ScheduledJob


def _llm_returning(classification: RunClassification) -> MagicMock:
    """A ``_build_structured_llm`` stand-in whose chain returns ``classification`` from ainvoke."""
    llm = MagicMock()
    llm.with_config.return_value.ainvoke = AsyncMock(return_value=classification)
    return llm


def _fake_site_settings(*, model="openrouter:primary", fallback="openrouter:fallback") -> MagicMock:
    ss = MagicMock()
    ss.run_classifier_model_name = model
    ss.run_classifier_fallback_model_name = fallback
    return ss


async def _make_scheduled_run(
    *,
    intent: str = Intent.WATCH_FIND,
    status: str = RunStatus.SUCCESSFUL,
    response_text: str = "The run finished.",
    error_message: str = "",
    with_schedule: bool = True,
) -> Run:
    """Build a ``ScheduledJob(intent=…) -> Session(scheduled_job=…) -> Run(session=…)`` chain.

    ``response_text`` is seeded via ``result_summary`` (the read-only ``Run.response_text``
    property falls back to it when no ``task_result`` is linked).
    """
    schedule = None
    if with_schedule:
        owner = await User.objects.acreate(username=f"owner-{uuid.uuid4()}", email=f"{uuid.uuid4()}@t.com")
        schedule = await ScheduledJob.objects.acreate(
            user=owner,
            name="nightly",
            prompt="check the repo",
            repos=[{"repo_id": "group/project", "ref": ""}],
            frequency=Frequency.DAILY,
            intent=intent,
        )
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="group/project", scheduled_job=schedule
    )
    return await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.SCHEDULE,
        repo_id="group/project",
        status=status,
        result_summary=response_text,
        error_message=error_message,
    )


@pytest.mark.django_db(transaction=True)
async def test_ac2_well_formed_envelope_for_watch_find():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND, response_text="Found a couple of problems.")
    classification = RunClassification(
        status="found-issues",
        summary="Two issues found in the auth module.",
        actionable=[
            ActionableDraft(
                kind="bug", label="Null deref in login", ref="auth/login.py", fix_prompt="Guard the None case."
            ),
            ActionableDraft(kind="todo", label="Missing test", ref="auth/tests.py"),
        ],
    )

    with patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.FOUND_ISSUES
    assert envelope.count == len(envelope.actionable) == 2
    assert envelope.summary == "Two issues found in the auth module."
    # Every item is contract-shaped: id + schema_version stamped by build_actionable_item.
    for item in envelope.actionable:
        assert set(item) >= {"id", "kind", "label", "ref", "schema_version"}
        assert isinstance(item["id"], str)
        assert item["schema_version"] == 1
    assert envelope.actionable[0]["fix_prompt"] == "Guard the None case."
    assert "fix_prompt" not in envelope.actionable[1]
    # Prose is retained and reachable (the classifier never touches the run's own result).
    reloaded = await Run.objects.aget(pk=run.pk)
    assert reloaded.response_text == "Found a couple of problems."


@pytest.mark.django_db(transaction=True)
async def test_ac3_report_intent_coerces_found_issues_to_needs_attention():
    run = await _make_scheduled_run(intent=Intent.REPORT)
    classification = RunClassification(
        status="found-issues",
        summary="Weekly summary of activity.",
        actionable=[ActionableDraft(kind="bug", label="x", ref="y")],
    )

    with patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.NEEDS_ATTENTION
    assert envelope.actionable == []
    assert envelope.count == 0


@pytest.mark.django_db(transaction=True)
async def test_ac3_report_intent_passes_all_clear_through_with_no_findings():
    run = await _make_scheduled_run(intent=Intent.REPORT)
    classification = RunClassification(
        status="all-clear", summary="Nothing notable.", actionable=[ActionableDraft(kind="bug", label="x", ref="y")]
    )

    with patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.ALL_CLEAR
    assert envelope.actionable == []
    assert envelope.count == 0


@pytest.mark.django_db(transaction=True)
async def test_ac4_found_issues_with_empty_list_coerced_to_all_clear():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(status="found-issues", summary="Odd but empty.", actionable=[])

    with patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.ALL_CLEAR
    assert envelope.actionable == []
    assert envelope.count == 0


@pytest.mark.django_db(transaction=True)
async def test_ac5_failed_run_is_failed_status_no_llm_call():
    # Non-empty prose on purpose: proves the FAILED gate wins over the classification path even when
    # there *is* text to classify (not merely over the empty-prose short-circuit).
    run = await _make_scheduled_run(
        status=RunStatus.FAILED,
        response_text="There is prose here that must NOT be classified.",
        error_message="Traceback: boom\nsecond line",
    )

    with patch("sessions.classification._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()  # no LLM/method call for a failed run
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.FAILED
    assert envelope.actionable == []
    assert envelope.count == 0
    # Summary is the first non-empty line of error_message, stated plainly (never a finding).
    assert envelope.summary == "Traceback: boom"


@pytest.mark.django_db(transaction=True)
async def test_ac5_failed_run_without_error_message_uses_generic_gloss():
    run = await _make_scheduled_run(status=RunStatus.FAILED, response_text="", error_message="")

    with patch("sessions.classification._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.FAILED
    assert envelope.summary == "Run failed."


@pytest.mark.django_db(transaction=True)
async def test_ac6_idempotent_writes_exactly_one_envelope():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(status="all-clear", summary="All good.", actionable=[])

    with patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)) as build:
        await classify_run_task.func(str(run.pk))
        await classify_run_task.func(str(run.pk))  # second call must no-op via the aexists guard

    assert await RunEnvelope.objects.filter(run=run).acount() == 1
    # Second call short-circuits before the method is invoked again.
    build.assert_called_once()


@pytest.mark.django_db(transaction=True)
async def test_ac6_pending_run_has_no_envelope():
    run = await _make_scheduled_run()
    # Task not run yet → envelope is absent ("classifying…"). ``for_run`` is sync-only, so it is
    # wrapped for this async test.
    envelope = await sync_to_async(RunEnvelope.objects.for_run)(run)
    assert envelope is None


@pytest.mark.django_db(transaction=True)
async def test_schedule_deleted_defaults_intent_to_watch_find():
    # A SCHEDULE-triggered run whose schedule was deleted (scheduled_job is None) still classifies;
    # intent defaults to WATCH_FIND, so found-issues + items is preserved as a finding.
    run = await _make_scheduled_run(with_schedule=False)
    classification = RunClassification(
        status="found-issues", summary="One problem.", actionable=[ActionableDraft(kind="bug", label="x", ref="y")]
    )

    with patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.FOUND_ISSUES
    assert envelope.count == 1


@pytest.mark.django_db(transaction=True)
async def test_no_model_configured_leaves_run_unclassified():
    run = await _make_scheduled_run()

    with (
        patch("core.site_settings.site_settings", _fake_site_settings(model="", fallback="")),
        patch("sessions.classification._build_structured_llm") as build,
    ):
        await classify_run_task.func(str(run.pk))  # must not raise

    build.assert_not_called()
    assert await sync_to_async(RunEnvelope.objects.for_run)(run) is None


@pytest.mark.django_db(transaction=True)
async def test_missing_run_is_a_clean_skip():
    with patch("sessions.classification._build_structured_llm") as build:
        await classify_run_task.func(str(uuid.uuid4()))  # must not raise

    build.assert_not_called()
    assert await RunEnvelope.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_method_failure_propagates_without_partial_envelope():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    failing = MagicMock()
    failing.with_config.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("upstream 500"))

    with patch("sessions.classification._build_structured_llm", return_value=failing), pytest.raises(RuntimeError):
        await classify_run_task.func(str(run.pk))

    assert await sync_to_async(RunEnvelope.objects.for_run)(run) is None


@pytest.mark.django_db(transaction=True)
async def test_non_report_all_clear_with_items_is_emptied():
    # Off-contract draft: a non-report run returns ``all-clear`` yet carries items. Only
    # ``found-issues`` may carry items, so the task empties them — status and actionable can never
    # disagree (the reverse of the AC4 invariant).
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(
        status="all-clear", summary="Nothing to do.", actionable=[ActionableDraft(kind="bug", label="x", ref="y")]
    )

    with patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.ALL_CLEAR
    assert envelope.actionable == []
    assert envelope.count == 0


@pytest.mark.django_db(transaction=True)
async def test_non_report_needs_attention_with_items_is_emptied():
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(
        status="needs-attention", summary="Take a look.", actionable=[ActionableDraft(kind="bug", label="x", ref="y")]
    )

    with patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.NEEDS_ATTENTION
    assert envelope.actionable == []
    assert envelope.count == 0


@pytest.mark.django_db(transaction=True)
async def test_successful_run_with_empty_prose_is_all_clear_without_llm_call():
    # A SUCCESSFUL run with empty prose (code-only run) has nothing to classify: write ``all-clear``
    # directly, never calling the method with an empty prompt.
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND, response_text="")

    with patch("sessions.classification._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.status == EnvelopeStatus.ALL_CLEAR
    assert envelope.actionable == []
    assert envelope.count == 0
    assert envelope.summary == ""


@pytest.mark.django_db(transaction=True)
async def test_forwards_run_prose_and_model_names_to_method():
    # Seam assertion: the run's prose and the primary-before-fallback model tuple (empties filtered)
    # actually reach the classification method — not just that the output happens to be right.
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND, response_text="classify this exact prose")
    llm = _llm_returning(RunClassification(status="all-clear", summary="ok", actionable=[]))

    with (
        patch("core.site_settings.site_settings", _fake_site_settings(model="primary", fallback="fallback")),
        patch("sessions.classification._build_structured_llm", return_value=llm) as build,
    ):
        await classify_run_task.func(str(run.pk))

    build.assert_called_once_with(RunClassification, ("primary", "fallback"))
    messages = llm.with_config.return_value.ainvoke.call_args.args[0]
    assert messages[1].content == "classify this exact prose"


@pytest.mark.django_db(transaction=True)
async def test_single_configured_model_forwards_one_tuple():
    # The empty-fallback is filtered out, so the method receives a 1-tuple (not an empty or 2-tuple).
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    llm = _llm_returning(RunClassification(status="all-clear", summary="ok", actionable=[]))

    with (
        patch("core.site_settings.site_settings", _fake_site_settings(model="only", fallback="")),
        patch("sessions.classification._build_structured_llm", return_value=llm) as build,
    ):
        await classify_run_task.func(str(run.pk))

    build.assert_called_once_with(RunClassification, ("only",))


@pytest.mark.django_db(transaction=True)
async def test_failed_run_summary_skips_leading_blank_lines():
    # First *non-empty* line, stripped — a regression to ``splitlines()[0]`` would yield "" here and
    # fall back to the generic gloss.
    run = await _make_scheduled_run(
        status=RunStatus.FAILED, response_text="", error_message="\n\n   Real error here   \n"
    )

    with patch("sessions.classification._build_structured_llm") as build:
        await classify_run_task.func(str(run.pk))

    build.assert_not_called()
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.summary == "Real error here"


@pytest.mark.django_db(transaction=True)
async def test_empty_fix_prompt_is_omitted_from_stored_item():
    # An off-contract empty ``fix_prompt`` is treated as absent, never stored as "" (which could seed a
    # downstream Finding -> Fix with no instruction).
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(
        status="found-issues",
        summary="one",
        actionable=[ActionableDraft(kind="bug", label="x", ref="y", fix_prompt="")],
    )

    with patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)):
        await classify_run_task.func(str(run.pk))

    envelope = await RunEnvelope.objects.aget(run=run)
    assert "fix_prompt" not in envelope.actionable[0]


@pytest.mark.django_db(transaction=True)
async def test_unexpected_integrity_error_propagates_and_writes_nothing():
    # A genuine IntegrityError (not the documented OneToOne race) must surface as a FAILED task rather
    # than being swallowed as a no-op — otherwise the run is left unclassified with no signal.
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(status="all-clear", summary="ok", actionable=[])

    with (
        patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)),
        patch.object(RunEnvelope.objects, "acreate", AsyncMock(side_effect=IntegrityError("boom"))),
        pytest.raises(IntegrityError),
    ):
        await classify_run_task.func(str(run.pk))

    # No envelope exists → the except re-checks aexists (False) and re-raises.
    assert await RunEnvelope.objects.filter(run=run).acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_persist_noops_when_race_winner_already_wrote_envelope():
    # The documented OneToOne race: the aexists guard passed, but a concurrent task wrote the envelope
    # before our acreate. The IntegrityError is caught, the re-check finds the winner's row, and we
    # no-op cleanly (no raise, exactly one envelope — the winner's).
    run = await _make_scheduled_run(intent=Intent.WATCH_FIND)
    classification = RunClassification(status="all-clear", summary="loser", actionable=[])

    async def _winner_then_raise(**kwargs):
        winner = RunEnvelope(run=run, status=EnvelopeStatus.ALL_CLEAR, count=0, summary="winner", actionable=[])
        await winner.asave()
        raise IntegrityError("duplicate key value violates unique constraint")

    with (
        patch("sessions.classification._build_structured_llm", return_value=_llm_returning(classification)),
        patch.object(RunEnvelope.objects, "acreate", AsyncMock(side_effect=_winner_then_raise)),
    ):
        await classify_run_task.func(str(run.pk))  # must not raise

    assert await RunEnvelope.objects.filter(run=run).acount() == 1
    envelope = await RunEnvelope.objects.aget(run=run)
    assert envelope.summary == "winner"
