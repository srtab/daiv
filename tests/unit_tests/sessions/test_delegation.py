import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from django_tasks_db.models import DBTaskResult
from sessions.artifacts import bind_active_run
from sessions.delegation import (
    MAX_CONTINUATION_ATTEMPTS,
    MAX_RESUMES_IN_A_ROW,
    MAX_STATUS_BATCHES,
    STALLED_BATCH_WINDOW,
    SessionJobDelegator,
    adelegator_for,
    leg_state,
    render_batch_summary,
    resume_coordinator,
    resume_stalled_batches,
)
from sessions.executor.lock import SessionLockTimeoutError
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.queue import LINK_FAILED_PREFIX

from automation.agent.delegation import MAX_DELEGATED_TARGETS, Checkout, DelegatedTarget, DelegationError
from automation.agent.references import RefIn
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied
from tests.unit_tests.sessions.conftest import make_task_result


def _coordinator(user=None, **fields) -> Session:
    defaults = {"origin": SessionOrigin.API_JOB, "repo_id": "acme/coord", "ref": "main", "user": user}
    return Session.objects.create(thread_id=str(uuid.uuid4()), **(defaults | fields))


def _delegating_run(coordinator: Session, user) -> Run:
    return Run.objects.create(
        session=coordinator,
        trigger_type=SessionOrigin.API_JOB,
        repo_id=coordinator.repo_id,
        status=RunStatus.RUNNING,
        user=user,
    )


def _leg(
    coordinator: Session, batch_id: uuid.UUID, *, status: str, task_result=None, session_ref: str = "", **fields
) -> Run:
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.DELEGATED_JOB,
        repo_id=fields.get("repo_id", "acme/api"),
        ref=session_ref,
        parent=coordinator,
    )
    fields = {"user": coordinator.user, "finished_at": timezone.now()} | fields
    return Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.DELEGATED_JOB,
        batch_id=batch_id,
        repo_id=session.repo_id,
        status=status,
        task_result=task_result,
        **{k: v for k, v in fields.items() if k != "repo_id"},
    )


def _target(repo_id: str = "acme/api", ref: str = "", prompt: str = "Bump auth to 3.2.1.") -> DelegatedTarget:
    return DelegatedTarget(repo_id=repo_id, ref=ref, prompt=prompt)


def _checkout(ref: str = "main") -> Checkout:
    return Checkout(repo_id="acme/coord", ref=ref)


async def _delegate(run: Run | None, targets, *, thread_id: str | None = None, checkout: Checkout | None = None):
    with bind_active_run(run.pk if run else None):
        return await SessionJobDelegator(thread_id or run.session_id).adelegate(
            checkout=checkout or _checkout(), goal="g", targets=targets
        )


def _mock_enqueue(mock_task, task_results):
    mock_task.aenqueue = AsyncMock(side_effect=[MagicMock(id=task.id) for task in task_results])


@pytest.mark.django_db(transaction=True)
class TestSessionJobDelegator:
    async def test_it_starts_one_unattended_leg_per_target_under_the_coordinator(
        self, admin_user, create_db_task_result
    ):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        run = await sync_to_async(_delegating_run)(coordinator, admin_user)
        tasks = [await sync_to_async(create_db_task_result)(status="READY") for _ in range(2)]

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, tasks)
            result = await _delegate(
                run, [_target("acme/api", prompt="Bump it."), _target("acme/web", "release", "Rebuild.")]
            )

        legs = [
            leg
            async for leg in Run.objects
            .filter(trigger_type=SessionOrigin.DELEGATED_JOB)
            .select_related("session")
            .order_by("repo_id")
        ]
        assert [(leg.repo_id, leg.ref, leg.prompt) for leg in legs] == [
            ("acme/api", "", "Bump it."),
            ("acme/web", "release", "Rebuild."),
        ]
        assert {leg.session.parent_id for leg in legs} == {coordinator.pk}
        assert {leg.session.origin for leg in legs} == {SessionOrigin.DELEGATED_JOB}
        assert {str(leg.batch_id) for leg in legs} == {result.batch_id}
        assert [call.kwargs["ask_user_enabled"] for call in mock_task.aenqueue.call_args_list] == [False, False]
        assert sorted((leg.repo_id, leg.thread_id) for leg in result.started) == [
            ("acme/api", legs[0].session_id),
            ("acme/web", legs[1].session_id),
        ]
        assert all(leg.session_url.startswith("https://") for leg in result.started)
        assert result.failed == []

    async def test_legs_run_as_the_user_who_triggered_the_run_not_the_sessions_creator(
        self, admin_user, member_user, create_db_task_result
    ):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        run = await sync_to_async(_delegating_run)(coordinator, member_user)
        task = await sync_to_async(create_db_task_result)(status="READY")
        checked = []

        with (
            patch("sessions.admission.assert_can_run", side_effect=lambda user, repo_ids: checked.append(user.pk)),
            patch("sessions.queue.run_job_task") as mock_task,
        ):
            _mock_enqueue(mock_task, [task])
            await _delegate(run, [_target()])

        leg = await Run.objects.aget(trigger_type=SessionOrigin.DELEGATED_JOB)
        assert leg.user_id == member_user.pk
        assert set(checked) == {member_user.pk}

    @pytest.mark.parametrize(
        ("setup", "targets", "message"),
        [
            ({"run_user": False}, [_target()], "has none"),
            ({}, [], "at least one target"),
            ({}, [_target(f"acme/r{i}") for i in range(MAX_DELEGATED_TARGETS + 1)], "At most"),
            ({}, [_target(prompt="")], "a prompt of its own"),
        ],
        ids=["no-user-behind-the-run", "no-targets", "too-many-targets", "blank-prompt"],
    )
    async def test_a_refused_call_starts_nothing(self, admin_user, setup, targets, message):
        coordinator = await sync_to_async(_coordinator)(admin_user, ref="")
        run = await sync_to_async(_delegating_run)(coordinator, admin_user if setup.get("run_user", True) else None)

        with patch("sessions.queue.run_job_task") as mock_task, pytest.raises(DelegationError, match=message):
            await _delegate(run, targets)

        mock_task.aenqueue.assert_not_called()
        assert not await Run.objects.filter(trigger_type=SessionOrigin.DELEGATED_JOB).aexists()

    async def test_a_tool_call_outside_a_bound_run_cannot_delegate(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)

        with pytest.raises(DelegationError, match="no session"):
            await _delegate(None, [_target()], thread_id=coordinator.pk)

    async def test_legs_may_share_a_base_the_coordinators_own_included_and_each_forks_its_branch(
        self, admin_user, create_db_task_result
    ):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        run = await sync_to_async(_delegating_run)(coordinator, admin_user)
        tasks = [await sync_to_async(create_db_task_result)(status="READY") for _ in range(3)]

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, tasks)
            result = await _delegate(
                run, [_target("acme/coord"), _target("acme/coord"), _target("ACME/Coord", "release")]
            )

        assert [(leg.repo_id, leg.ref, leg.starts_after_this_turn) for leg in result.started] == [
            ("acme/coord", "", False),
            ("acme/coord", "", False),
            ("ACME/Coord", "release", False),
        ]
        assert [call.kwargs["fork_branch"] for call in mock_task.aenqueue.call_args_list] == [True, True, True]

    @pytest.mark.parametrize("trigger_type", [SessionOrigin.API_JOB, SessionOrigin.CHAT])
    async def test_a_leg_on_the_coordinators_feature_branch_starts_once_the_delegating_run_has_finished(
        self, admin_user, create_db_task_result, trigger_type
    ):
        coordinator = await sync_to_async(_coordinator)(admin_user, ref="feat/x")
        run = await sync_to_async(_delegating_run)(coordinator, admin_user)
        await Run.objects.filter(pk=run.pk).aupdate(trigger_type=trigger_type)
        run.trigger_type = trigger_type
        tasks = [await sync_to_async(create_db_task_result)(status="READY") for _ in range(2)]

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, tasks)
            result = await _delegate(
                run, [_target("acme/coord", "feat/x"), _target("acme/coord")], checkout=_checkout(ref="feat/x")
            )
            stacked = await Run.objects.aget(trigger_type=SessionOrigin.DELEGATED_JOB, ref="feat/x")
            assert (stacked.status, stacked.waits_for_id, stacked.task_result_id) == (RunStatus.QUEUED, run.pk, None)
            assert mock_task.aenqueue.await_count == 1

            await Run.objects.afinish(run, RunStatus.SUCCESSFUL, finished_at=timezone.now())

        await stacked.arefresh_from_db()
        assert (stacked.status, stacked.task_result_id) == (RunStatus.READY, tasks[1].id)
        assert [leg.starts_after_this_turn for leg in result.started] == [True, False]

    async def test_a_waiting_leg_moves_to_the_branch_the_coordinator_published_to(
        self, admin_user, create_db_task_result
    ):
        coordinator = await sync_to_async(_coordinator)(admin_user, ref="feat/x")
        run = await sync_to_async(_delegating_run)(coordinator, admin_user)
        task = await sync_to_async(create_db_task_result)(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, [task])
            result = await _delegate(run, [_target("acme/coord", "feat/x")], checkout=_checkout(ref="feat/x"))
            await Session.objects.filter(pk=coordinator.pk).aupdate(ref="feat/x-published")
            await Run.objects.afinish(run, RunStatus.SUCCESSFUL, finished_at=timezone.now())

        leg = await Run.objects.select_related("session").aget(session_id=result.started[0].thread_id)
        assert (leg.ref, leg.session.ref) == ("feat/x-published", "feat/x-published")
        assert mock_task.aenqueue.call_args.kwargs["ref"] == "feat/x-published"

    async def test_a_legs_references_are_linked_into_its_session(self, admin_user, create_db_task_result):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        run = await sync_to_async(_delegating_run)(coordinator, admin_user)
        task = await sync_to_async(create_db_task_result)(status="READY")
        sentry = RefIn(key="DAIV-1V", provider="sentry", url="https://sentry.example/issues/1", relation="closes")

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, [task])
            result = await _delegate(
                run, [DelegatedTarget(repo_id="acme/api", ref="", prompt="Fix it.", references=(sentry,))]
            )

        session = await Session.objects.aget(pk=result.started[0].thread_id)
        assert [ref.key for ref in session.external_references()] == ["DAIV-1V"]

    async def test_targets_the_user_cannot_write_to_fail_while_the_rest_start(self, member_user, create_db_task_result):
        coordinator = await sync_to_async(_coordinator)(member_user)
        run = await sync_to_async(_delegating_run)(coordinator, member_user)
        task = await sync_to_async(create_db_task_result)(status="READY")

        def can_run(user, repo_ids):
            if denied := [repo_id for repo_id in repo_ids if repo_id == "acme/secret"]:
                raise RepositoryAccessDenied(denied)

        with patch("sessions.admission.assert_can_run", side_effect=can_run), patch("sessions.queue.run_job_task") as t:
            _mock_enqueue(t, [task])
            result = await _delegate(run, [_target("acme/api"), _target("acme/secret")])

        assert [leg.repo_id for leg in result.started] == ["acme/api"]
        assert [(f.repo_id, f.error) for f in result.failed] == [("acme/secret", REPO_ACCESS_DENIED_MESSAGE)]

    async def test_when_every_target_is_denied_no_batch_is_made(self, member_user):
        coordinator = await sync_to_async(_coordinator)(member_user)
        run = await sync_to_async(_delegating_run)(coordinator, member_user)

        with patch("sessions.admission.assert_can_run", side_effect=RepositoryAccessDenied(["acme/api"])):
            result = await _delegate(run, [_target()])

        assert (result.batch_id, result.started) == (None, [])
        assert [f.repo_id for f in result.failed] == ["acme/api"]
        assert not await Run.objects.filter(trigger_type=SessionOrigin.DELEGATED_JOB).aexists()

    async def test_a_leg_that_cannot_be_queued_is_reported_failed(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        run = await sync_to_async(_delegating_run)(coordinator, admin_user)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=RuntimeError("broker down"))
            result = await _delegate(run, [_target()])

        assert result.started == []
        assert [(f.repo_id, f.still_runs) for f in result.failed] == [("acme/api", False)]
        assert "broker down" not in result.failed[0].error
        leg = await Run.objects.aget(trigger_type=SessionOrigin.DELEGATED_JOB)
        assert "broker down" in leg.error_message

    async def test_a_leg_whose_task_was_queued_but_not_linked_is_reported_as_still_running(
        self, admin_user, create_db_task_result
    ):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        run = await sync_to_async(_delegating_run)(coordinator, admin_user)
        task = await sync_to_async(create_db_task_result)(status="READY")

        with (
            patch("sessions.queue.run_job_task") as mock_task,
            patch.object(Run, "asave", AsyncMock(side_effect=RuntimeError("db blip"))),
        ):
            _mock_enqueue(mock_task, [task])
            result = await _delegate(run, [_target()])

        assert result.started == []
        [failed] = result.failed
        assert failed.still_runs is True
        assert "db blip" not in failed.error

    async def test_legs_inherit_the_delegating_runs_mute(self, admin_user, create_db_task_result):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        run = await sync_to_async(_delegating_run)(coordinator, admin_user)
        await Run.objects.filter(pk=run.pk).aupdate(muted=True)
        task = await sync_to_async(create_db_task_result)(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, [task])
            await _delegate(run, [_target()])

        assert await Run.objects.filter(trigger_type=SessionOrigin.DELEGATED_JOB).values_list("muted", flat=True).aget()

    async def test_a_leg_on_the_default_branch_the_coordinator_is_on_waits_and_moves_to_what_it_published(
        self, admin_user, create_db_task_result
    ):
        coordinator = await sync_to_async(_coordinator)(admin_user, ref="main")
        run = await sync_to_async(_delegating_run)(coordinator, admin_user)
        tasks = [await sync_to_async(create_db_task_result)(status="READY") for _ in range(2)]

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, tasks)
            result = await _delegate(run, [_target("acme/coord", "main"), _target("acme/coord")])
            assert mock_task.aenqueue.await_count == 1
            await Session.objects.filter(pk=coordinator.pk).aupdate(ref="daiv/coordinator-work")
            await Run.objects.afinish(run, RunStatus.SUCCESSFUL, finished_at=timezone.now())

        assert [(leg.ref, leg.starts_after_this_turn) for leg in result.started] == [("main", True), ("", False)]
        assert mock_task.aenqueue.call_args.kwargs["ref"] == "daiv/coordinator-work"

    @pytest.mark.parametrize(("resumes", "refused"), [(MAX_RESUMES_IN_A_ROW - 1, False), (MAX_RESUMES_IN_A_ROW, True)])
    async def test_a_coordinator_resumed_too_many_times_in_a_row_cannot_delegate_again(
        self, admin_user, create_db_task_result, resumes, refused
    ):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        await sync_to_async(_delegating_run)(coordinator, admin_user)
        await Run.objects.filter(session=coordinator).aupdate(status=RunStatus.SUCCESSFUL)
        for attempt in range(resumes):
            run = await Run.objects.acreate(
                session=coordinator,
                trigger_type=SessionOrigin.DELEGATION_RESUME,
                repo_id="acme/coord",
                status=RunStatus.RUNNING if attempt == resumes - 1 else RunStatus.SUCCESSFUL,
                user=admin_user,
                continuation_of_batch_id=uuid.uuid4(),
            )
        run = await Run.objects.select_related("session", "user").aget(pk=run.pk)
        task = await sync_to_async(create_db_task_result)(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, [task])
            if refused:
                with pytest.raises(DelegationError, match="times in a row"):
                    await _delegate(run, [_target()])
            else:
                assert (await _delegate(run, [_target()])).started


@pytest.mark.django_db(transaction=True)
async def test_only_a_session_that_was_not_delegated_gets_a_delegator(admin_user):
    coordinator = await sync_to_async(_coordinator)(admin_user)
    leg = await sync_to_async(_coordinator)(admin_user, parent=coordinator)

    assert isinstance(await adelegator_for(coordinator.pk), SessionJobDelegator)
    assert await adelegator_for(leg.pk) is None


@pytest.mark.django_db(transaction=True)
class TestResumeCoordinator:
    def test_once_every_leg_finished_it_queues_one_summarised_run_on_the_coordinator_and_releases_it(
        self, admin_user, member_user, create_db_task_result, monkeypatch
    ):
        monkeypatch.setattr("automation.agent.validators.validate_agent_override", lambda model, level: (model, level))
        coordinator = _coordinator(admin_user, agent_model="m", agent_thinking_level="high")
        batch_id = uuid.uuid4()
        done = _leg(
            coordinator,
            batch_id,
            status=RunStatus.SUCCESSFUL,
            task_result=create_db_task_result(),
            result_summary="Bumped.",
            merge_request_web_url="https://git.example/mr/1",
            user=member_user,
        )
        failed = _leg(
            coordinator,
            batch_id,
            status=RunStatus.FAILED,
            task_result=create_db_task_result(),
            repo_id="b",
            user=member_user,
        )
        task = create_db_task_result(status="READY")

        with patch("sessions.admission.assert_can_run"), patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            resume_coordinator(failed)

        continuation = Run.objects.get(continuation_of_batch_id=batch_id)
        assert continuation.session_id == coordinator.pk
        assert (continuation.trigger_type, continuation.status, continuation.task_result_id) == (
            SessionOrigin.DELEGATION_RESUME,
            RunStatus.READY,
            task.id,
        )
        assert (continuation.repo_id, continuation.ref, continuation.user_id) == ("acme/coord", "main", member_user.pk)
        assert (continuation.agent_model, continuation.agent_thinking_level) == ("m", "high")
        assert continuation.prompt == render_batch_summary(batch_id, [done, failed])
        assert mock_task.aenqueue.call_args.kwargs["thread_id"] == coordinator.pk
        assert mock_task.aenqueue.call_args.kwargs["ask_user_enabled"] is True

    @pytest.mark.parametrize(
        ("origin", "can_ask"), [(SessionOrigin.SCHEDULE, False), (SessionOrigin.PIPELINE_WEBHOOK, False)]
    )
    def test_the_continuation_of_an_unattended_coordinator_cannot_ask(
        self, admin_user, create_db_task_result, origin, can_ask
    ):
        coordinator = _coordinator(admin_user, origin=origin)
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            resume_coordinator(leg)

        assert mock_task.aenqueue.call_args.kwargs["ask_user_enabled"] is can_ask

    def test_the_continuation_keeps_the_legs_mute(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result(), muted=True)

        with patch("sessions.queue.run_job_task"):
            resume_coordinator(leg)

        assert Run.objects.get(continuation_of_batch_id=batch_id).muted is True

    def test_it_waits_while_a_leg_is_still_running(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        finished = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        _leg(coordinator, batch_id, status=RunStatus.RUNNING, task_result=create_db_task_result(status="RUNNING"))

        resume_coordinator(finished)

        assert not Run.objects.filter(continuation_of_batch_id=batch_id).exists()

    def test_a_batch_resumes_its_coordinator_once(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            resume_coordinator(leg)
            resume_coordinator(leg)

        assert Run.objects.filter(continuation_of_batch_id=batch_id).count() == 1

    def test_a_continuation_that_ran_and_failed_is_not_retried(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        Run.objects.create(
            session=coordinator,
            trigger_type=SessionOrigin.DELEGATION_RESUME,
            repo_id="acme/coord",
            status=RunStatus.FAILED,
            error_message="the agent raised",
            continuation_of_batch_id=batch_id,
        )

        resume_coordinator(leg)

        assert Run.objects.filter(continuation_of_batch_id=batch_id).count() == 1

    def test_a_batch_whose_legs_never_reached_the_queue_resumes_nothing(self, admin_user):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.FAILED)

        resume_coordinator(leg)

        assert not Run.objects.filter(continuation_of_batch_id=batch_id).exists()

    def test_a_batch_whose_waiting_legs_never_reached_the_queue_still_resumes(self, admin_user):
        coordinator = _coordinator(admin_user, ref="feat/x")
        delegating = _delegating_run(coordinator, admin_user)
        Run.objects.filter(pk=delegating.pk).update(status=RunStatus.SUCCESSFUL)
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.FAILED, waits_for=delegating)

        with patch("sessions.queue.run_job_task"):
            resume_coordinator(leg)

        assert Run.objects.filter(continuation_of_batch_id=batch_id).exists()

    def test_a_busy_coordinator_keeps_its_continuation_queued(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        _delegating_run(coordinator, admin_user)
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())

        with patch("sessions.queue.run_job_task") as mock_task:
            resume_coordinator(leg)

        mock_task.aenqueue.assert_not_called()
        assert Run.objects.get(continuation_of_batch_id=batch_id).status == RunStatus.QUEUED

    def test_the_continuation_waits_for_a_chat_turn_in_flight_and_starts_once_it_ends_on_its_branch(
        self, admin_user, create_db_task_result
    ):
        coordinator = _coordinator(admin_user)
        chat = Run.objects.create(
            session=coordinator, trigger_type=SessionOrigin.CHAT, repo_id="acme/coord", status=RunStatus.RUNNING
        )
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            resume_coordinator(leg)
            continuation = Run.objects.get(continuation_of_batch_id=batch_id)
            assert (continuation.status, continuation.waits_for_id) == (RunStatus.QUEUED, chat.pk)
            mock_task.aenqueue.assert_not_called()

            Session.objects.filter(pk=coordinator.pk).update(ref="daiv/chat-work")
            Run.objects.finish(chat, RunStatus.SUCCESSFUL, finished_at=timezone.now())

        continuation.refresh_from_db()
        assert (continuation.status, continuation.task_result_id) == (RunStatus.READY, task.id)
        assert mock_task.aenqueue.call_args.kwargs["ref"] == "daiv/chat-work"

    @pytest.mark.parametrize("legs_user", ["lost-access", "deleted"])
    def test_a_user_who_can_no_longer_write_to_the_coordinator_gets_it_failed_not_run(
        self, member_user, create_db_task_result, legs_user
    ):
        coordinator = _coordinator(member_user)
        batch_id = uuid.uuid4()
        leg = _leg(
            coordinator,
            batch_id,
            status=RunStatus.SUCCESSFUL,
            task_result=create_db_task_result(),
            user=member_user if legs_user == "lost-access" else None,
        )

        with (
            patch("sessions.admission.assert_can_run", side_effect=RepositoryAccessDenied(["acme/coord"])),
            patch("sessions.queue.run_job_task") as mock_task,
        ):
            resume_coordinator(leg)
            resume_stalled_batches()

        mock_task.aenqueue.assert_not_called()
        continuation = Run.objects.get(continuation_of_batch_id=batch_id)
        assert continuation.status == RunStatus.FAILED
        assert continuation.error_message.startswith("access_denied: ")

    def test_a_denied_continuation_is_recorded_while_the_coordinator_is_busy_with_runs_queued(
        self, member_user, create_db_task_result
    ):
        coordinator = _coordinator(member_user)
        _delegating_run(coordinator, member_user)
        Run.objects.create(
            session=coordinator, trigger_type=SessionOrigin.API_JOB, repo_id="acme/coord", status=RunStatus.QUEUED
        )
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())

        with (
            patch("sessions.admission.assert_can_run", side_effect=RepositoryAccessDenied(["acme/coord"])),
            patch("sessions.queue.run_job_task"),
        ):
            resume_coordinator(leg)

        assert Run.objects.get(continuation_of_batch_id=batch_id).status == RunStatus.FAILED

    @pytest.mark.parametrize(
        "fields", [{"trigger_type": SessionOrigin.API_JOB}, {"batch_id": None}], ids=["not-delegated", "no-batch"]
    )
    def test_only_a_delegated_batch_resumes_anything(self, admin_user, create_db_task_result, fields):
        coordinator = _coordinator(admin_user)
        leg = _leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        for name, value in fields.items():
            setattr(leg, name, value)

        resume_coordinator(leg)

        assert not Run.objects.filter(continuation_of_batch_id__isnull=False).exists()

    def test_a_batch_whose_coordinator_is_gone_resumes_nothing(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        coordinator.delete()

        resume_coordinator(leg)

        assert not Run.objects.filter(continuation_of_batch_id=batch_id).exists()


@pytest.mark.django_db(transaction=True)
class TestResumeStalledBatches:
    def test_a_finished_batch_left_without_a_continuation_gets_one(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            assert resume_stalled_batches() == 1

        assert Run.objects.get(continuation_of_batch_id=batch_id).task_result_id == task.id

    def test_a_continuation_that_never_reached_the_queue_is_retried_a_bounded_number_of_times(
        self, admin_user, create_db_task_result
    ):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=RuntimeError("broker down"))
            resume_coordinator(leg)
            for _ in range(MAX_CONTINUATION_ATTEMPTS + 1):
                resume_stalled_batches()

        continuations = Run.objects.filter(continuation_of_batch_id=batch_id)
        assert continuations.count() == MAX_CONTINUATION_ATTEMPTS
        assert set(continuations.values_list("status", flat=True)) == {RunStatus.FAILED}

    def test_a_continuation_that_gave_up_waiting_for_the_coordinators_slot_is_retried(
        self, admin_user, create_db_task_result
    ):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        Run.objects.create(
            session=coordinator,
            trigger_type=SessionOrigin.DELEGATION_RESUME,
            repo_id="acme/coord",
            status=RunStatus.FAILED,
            error_message=f"{SessionLockTimeoutError.__module__}.{SessionLockTimeoutError.__qualname__}\nTraceback",
            continuation_of_batch_id=batch_id,
        )
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            assert resume_stalled_batches() == 1

        assert Run.objects.filter(continuation_of_batch_id=batch_id, task_result_id=task.id).exists()

    def test_a_leg_left_queued_behind_a_finished_run_is_restacked_and_started(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user, ref="daiv/published")
        delegating = _delegating_run(coordinator, admin_user)
        Run.objects.filter(pk=delegating.pk).update(status=RunStatus.SUCCESSFUL)
        leg = _leg(
            coordinator, uuid.uuid4(), status=RunStatus.QUEUED, ref="main", waits_for=delegating, finished_at=None
        )
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            resume_stalled_batches()

        leg.refresh_from_db()
        assert (leg.status, leg.ref, leg.task_result_id) == (RunStatus.READY, "daiv/published", task.id)

    def test_a_batch_that_finished_before_the_window_is_left_alone(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        long_ago = timezone.now() - STALLED_BATCH_WINDOW - timedelta(minutes=1)
        _leg(
            coordinator,
            batch_id,
            status=RunStatus.SUCCESSFUL,
            task_result=create_db_task_result(),
            finished_at=long_ago,
        )

        with patch("sessions.queue.run_job_task") as mock_task:
            assert resume_stalled_batches() == 0

        mock_task.aenqueue.assert_not_called()

    def test_an_unfinished_batch_is_left_alone(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        _leg(coordinator, batch_id, status=RunStatus.RUNNING, task_result=create_db_task_result(status="RUNNING"))

        assert resume_stalled_batches() == 0
        assert not Run.objects.filter(continuation_of_batch_id=batch_id).exists()

    def test_a_continuation_left_queued_on_a_free_coordinator_is_released(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        continuation = Run.objects.create(
            session=coordinator,
            trigger_type=SessionOrigin.DELEGATION_RESUME,
            repo_id="acme/coord",
            status=RunStatus.QUEUED,
            continuation_of_batch_id=uuid.uuid4(),
        )
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            resume_stalled_batches()

        continuation.refresh_from_db()
        assert (continuation.status, continuation.task_result_id) == (RunStatus.READY, task.id)


@pytest.mark.django_db(transaction=True)
class TestStatus:
    async def test_it_reports_each_leg_of_a_batch_without_daivs_error_text(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        batch_id = uuid.uuid4()
        await sync_to_async(_leg)(
            coordinator,
            batch_id,
            status=RunStatus.SUCCESSFUL,
            ref="release",
            session_ref="fix/bump-auth",
            result_summary=" Bumped. ",
            merge_request_web_url="https://git.example/mr/1",
        )
        await sync_to_async(_leg)(
            coordinator,
            batch_id,
            status=RunStatus.FAILED,
            repo_id="acme/web",
            error_message="enqueue_failed: OperationalError: host db-internal:5432",
        )
        await sync_to_async(_leg)(coordinator, batch_id, status=RunStatus.RUNNING, repo_id="acme/ops", finished_at=None)

        [batch] = await SessionJobDelegator(coordinator.pk).astatus(batch_id=str(batch_id))

        assert (batch.batch_id, batch.finished) == (str(batch_id), False)
        assert [(leg.repo_id, leg.status) for leg in batch.legs] == [
            ("acme/api", "succeeded"),
            ("acme/web", "failed"),
            ("acme/ops", "running"),
        ]
        done = batch.legs[0]
        assert (done.ref, done.branch, done.merge_request_url, done.reply) == (
            "release",
            "fix/bump-auth",
            "https://git.example/mr/1",
            "Bumped.",
        )
        assert done.session_url.endswith(f"/sessions/{done.thread_id}/")
        assert "db-internal" not in repr(batch)

    async def test_without_a_batch_it_reports_the_latest_batches_of_this_session_newest_first(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        other = await sync_to_async(_coordinator)(admin_user)
        batch_ids = [uuid.uuid4() for _ in range(MAX_STATUS_BATCHES + 1)]
        for age, batch_id in enumerate(reversed(batch_ids)):
            await sync_to_async(_leg)(
                coordinator, batch_id, status=RunStatus.SUCCESSFUL, created_at=timezone.now() - timedelta(hours=age)
            )
        await sync_to_async(_leg)(other, uuid.uuid4(), status=RunStatus.SUCCESSFUL)

        batches = await SessionJobDelegator(coordinator.pk).astatus(batch_id=None)

        assert [batch.batch_id for batch in batches] == [str(b) for b in reversed(batch_ids[1:])]
        assert all(batch.finished for batch in batches)

    async def test_a_session_that_delegated_nothing_has_no_batches(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)

        assert await SessionJobDelegator(coordinator.pk).astatus(batch_id=None) == []

    @pytest.mark.parametrize("batch_id", ["another-sessions", "not-a-uuid"])
    async def test_it_refuses_a_batch_this_session_did_not_delegate(self, admin_user, batch_id):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        foreign = uuid.uuid4()
        await sync_to_async(_leg)(await sync_to_async(_coordinator)(admin_user), foreign, status=RunStatus.SUCCESSFUL)

        with pytest.raises(DelegationError):
            await SessionJobDelegator(coordinator.pk).astatus(
                batch_id=str(foreign) if batch_id == "another-sessions" else batch_id
            )


def _running_leg(coordinator: Session, batch_id: uuid.UUID) -> Run:
    task_result = make_task_result(status="RUNNING")
    return _leg(coordinator, batch_id, status=RunStatus.RUNNING, task_result=task_result, finished_at=None)


def _end_task_as_its_worker_would(leg: Run, *, status: str, **task_fields) -> None:
    DBTaskResult.objects.filter(pk=leg.task_result_id).update(status=status, finished_at=timezone.now(), **task_fields)
    Run.objects.select_related("task_result", "session").get(pk=leg.pk).sync_and_save()


@pytest.mark.django_db(transaction=True)
class TestCancel:
    async def test_it_finishes_a_queued_leg_and_asks_a_running_one_to_stop(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        batch_id = uuid.uuid4()
        done = await sync_to_async(_leg)(
            coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=await sync_to_async(make_task_result)()
        )
        queued = await sync_to_async(_leg)(coordinator, batch_id, status=RunStatus.QUEUED, finished_at=None)
        running = await sync_to_async(_running_leg)(coordinator, batch_id)

        with patch("sessions.queue.run_job_task"):
            result = await SessionJobDelegator(coordinator.pk).acancel(batch_id=str(batch_id), thread_ids=[])

        assert result.cancelled == [queued.session_id, running.session_id]
        assert [leg.status for leg in result.batch.legs] == ["succeeded", "cancelled", "stopping"]
        assert (result.batch.finished, result.resumes) == (False, True)
        await queued.arefresh_from_db()
        assert queued.is_cancelled
        await running.arefresh_from_db()
        assert (running.status, running.cancel_requested_at is not None) == (RunStatus.RUNNING, True)
        await done.arefresh_from_db()
        assert done.status == RunStatus.SUCCESSFUL
        assert not await Run.objects.filter(continuation_of_batch_id=batch_id).aexists()

    async def test_once_a_stopping_leg_stops_the_coordinator_resumes_after_its_turn(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        turn = await sync_to_async(_delegating_run)(coordinator, admin_user)
        batch_id = uuid.uuid4()
        await sync_to_async(_leg)(coordinator, batch_id, status=RunStatus.SUCCESSFUL)
        running = await sync_to_async(_running_leg)(coordinator, batch_id)

        with patch("sessions.queue.run_job_task"):
            await SessionJobDelegator(coordinator.pk).acancel(batch_id=str(batch_id), thread_ids=[running.session_id])
            await sync_to_async(_end_task_as_its_worker_would)(
                running, status="FAILED", exception_class_path="sessions.executor.run.RunStoppedError"
            )

        await running.arefresh_from_db()
        assert running.is_cancelled
        continuation = await Run.objects.aget(continuation_of_batch_id=batch_id)
        assert (continuation.status, continuation.waits_for_id) == (RunStatus.QUEUED, turn.pk)
        assert "## acme/api: cancelled" in continuation.prompt

    async def test_a_stopping_leg_that_finishes_first_keeps_its_merge_request(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        await sync_to_async(_delegating_run)(coordinator, admin_user)
        batch_id = uuid.uuid4()
        running = await sync_to_async(_running_leg)(coordinator, batch_id)

        with patch("sessions.queue.run_job_task"):
            result = await SessionJobDelegator(coordinator.pk).acancel(batch_id=str(batch_id), thread_ids=[])
            await sync_to_async(_end_task_as_its_worker_would)(
                running,
                status="SUCCESSFUL",
                return_value={"response": "Opened it.", "merge_request_web_url": "https://git.example/mr/3"},
            )

        assert result.resumes is False
        [leg] = (await SessionJobDelegator(coordinator.pk).astatus(batch_id=str(batch_id)))[0].legs
        assert (leg.status, leg.merge_request_url) == ("succeeded", "https://git.example/mr/3")
        continuation = await Run.objects.aget(continuation_of_batch_id=batch_id)
        assert "- Merge request: https://git.example/mr/3" in continuation.prompt

    async def test_a_batch_whose_every_leg_was_cancelled_resumes_nothing(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        batch_id = uuid.uuid4()
        legs = [await sync_to_async(_running_leg)(coordinator, batch_id) for _ in range(2)]

        with patch("sessions.queue.run_job_task"):
            result = await SessionJobDelegator(coordinator.pk).acancel(batch_id=str(batch_id), thread_ids=[])
            for leg in legs:
                await sync_to_async(_end_task_as_its_worker_would)(
                    leg, status="FAILED", exception_class_path="sessions.executor.run.RunStoppedError"
                )
            stalled = await sync_to_async(resume_stalled_batches)()

        assert len(result.cancelled) == 2
        assert (result.batch.finished, result.resumes) == (False, False)
        assert stalled == 0
        assert not await Run.objects.filter(continuation_of_batch_id=batch_id).aexists()

    async def test_it_cancels_only_the_named_legs(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        batch_id = uuid.uuid4()
        named, other = [await sync_to_async(_running_leg)(coordinator, batch_id) for _ in range(2)]

        delegator = SessionJobDelegator(coordinator.pk)

        result = await delegator.acancel(batch_id=str(batch_id), thread_ids=[named.session_id])

        assert result.cancelled == [named.session_id]
        assert [leg.status for leg in result.batch.legs] == ["stopping", "running"]
        assert (result.batch.finished, result.resumes) == (False, True)
        await other.arefresh_from_db()
        assert (other.status, other.cancel_requested_at) == (RunStatus.RUNNING, None)

    async def test_a_finished_leg_is_left_as_it_ended(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        batch_id = uuid.uuid4()
        done = await sync_to_async(_leg)(coordinator, batch_id, status=RunStatus.SUCCESSFUL)

        result = await SessionJobDelegator(coordinator.pk).acancel(batch_id=str(batch_id), thread_ids=[done.session_id])

        assert result.cancelled == []
        assert [leg.status for leg in result.batch.legs] == ["succeeded"]

    async def test_a_leg_outside_the_batch_refuses_the_whole_call(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        batch_id = uuid.uuid4()
        leg = await sync_to_async(_leg)(coordinator, batch_id, status=RunStatus.RUNNING, finished_at=None)
        stranger = await sync_to_async(_leg)(coordinator, uuid.uuid4(), status=RunStatus.RUNNING, finished_at=None)

        with pytest.raises(DelegationError, match=stranger.session_id):
            await SessionJobDelegator(coordinator.pk).acancel(
                batch_id=str(batch_id), thread_ids=[leg.session_id, stranger.session_id]
            )

        await leg.arefresh_from_db()
        assert leg.status == RunStatus.RUNNING


@pytest.mark.django_db
def test_the_summary_lists_each_legs_outcome_with_its_reply_quoted(admin_user):
    coordinator = _coordinator(admin_user)
    batch_id = uuid.uuid4()
    legs = [
        _leg(
            coordinator,
            batch_id,
            status=RunStatus.SUCCESSFUL,
            ref="release",
            session_ref="fix/bump-auth",
            result_summary="Bumped.\n## acme/web: succeeded",
            merge_request_web_url="https://git.example/mr/1",
        ),
        _leg(
            coordinator,
            batch_id,
            status=RunStatus.FAILED,
            repo_id="acme/web",
            error_message="enqueue_failed: OperationalError: host db-internal:5432",
        ),
        _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, repo_id="acme/ops", result_summary="x" * 5000),
        _leg(coordinator, batch_id, status=RunStatus.FAILED, repo_id="acme/lost", error_message=LINK_FAILED_PREFIX),
        _leg(coordinator, batch_id, status=RunStatus.FAILED, repo_id="acme/void", cancel_requested_at=timezone.now()),
    ]

    summary = render_batch_summary(batch_id, legs)

    assert summary.startswith(f"The sub-jobs you delegated (batch {batch_id}) have finished: 2 of 5 succeeded.")
    assert "## acme/api on release: succeeded" in summary
    assert "- Branch: fix/bump-auth" in summary
    assert "- Merge request: https://git.example/mr/1" in summary
    assert "  > Bumped.\n  > ## acme/web: succeeded" in summary
    assert "## acme/web: failed\n" in summary
    assert "db-internal" not in summary
    assert "- Reply: none; open its session to see what happened." in summary
    assert "  > " + "x" * 1000 + "\n" in summary
    assert "## acme/lost: started, but DAIV lost track of it" in summary
    assert "## acme/void: cancelled\n- Session:" in summary
    assert summary.count("- Reply: none") == 2
    assert "this is not their answer" in summary
    assert f"/sessions/{legs[0].session_id}/" in summary


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("fields", "waits", "expected"),
    [
        ({"status": RunStatus.QUEUED}, False, "queued"),
        ({"status": RunStatus.QUEUED}, True, "waits"),
        ({"status": RunStatus.READY}, False, "queued"),
        ({"status": RunStatus.RUNNING}, False, "running"),
        ({"status": RunStatus.WAITING_INPUT}, False, "waiting_input"),
        ({"status": RunStatus.SUCCESSFUL}, False, "finished"),
        ({"status": RunStatus.FAILED}, False, "failed"),
        ({"status": RunStatus.FAILED, "error_message": f"{LINK_FAILED_PREFIX}: x"}, False, "lost"),
        ({"status": RunStatus.FAILED, "cancel_requested_at": timezone.now()}, False, "cancelled"),
        ({"status": RunStatus.RUNNING, "cancel_requested_at": timezone.now()}, False, "stopping"),
    ],
)
def test_leg_state(admin_user, fields, waits, expected):
    coordinator = _coordinator(admin_user)
    if waits:
        fields = fields | {"waits_for": _delegating_run(coordinator, admin_user)}

    assert leg_state(_leg(coordinator, uuid.uuid4(), **fields)) == expected
