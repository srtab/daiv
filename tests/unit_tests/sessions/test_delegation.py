import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from django_tasks_db.models import DBTaskResult
from sessions.artifacts import bind_active_run
from sessions.delegation import (
    _NOT_A_LEG,
    _UNSTARTED_LEG,
    MAX_STATUS_BATCHES,
    STALLED_BATCH_WINDOW,
    SessionJobDelegator,
    adelegator_for,
    leg_state,
    load_delegations,
    outcome_states,
    render_batch_summary,
    resume_coordinator,
    resume_stalled_batches,
)
from sessions.inbox.drain import Drain
from sessions.inbox.leg_follow_ups import BUSY, LegFollowUpRefused
from sessions.inbox.ownership import Ownerships
from sessions.legs import NOT_RUN_CLOSED, NOT_RUN_MERGED
from sessions.models import (
    LINK_FAILED_PREFIX,
    EventKind,
    EventState,
    HiddenSession,
    InboxEvent,
    MergeRequestOwnership,
    MergeRequestState,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)

from automation.agent.delegation import MAX_DELEGATED_TARGETS, Checkout, DelegatedTarget, DelegationError, StartedLeg
from automation.agent.references import RefIn
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied
from tests.unit_tests.sessions.conftest import make_coordinator, make_delegating_run, make_leg, make_task_result
from tests.unit_tests.sessions.inbox.conftest import make_event, post_leg_follow_up


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
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        run = await sync_to_async(make_delegating_run)(coordinator, admin_user)
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
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        run = await sync_to_async(make_delegating_run)(coordinator, member_user)
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
        coordinator = await sync_to_async(make_coordinator)(admin_user, ref="")
        run = await sync_to_async(make_delegating_run)(coordinator, admin_user if setup.get("run_user", True) else None)

        with patch("sessions.queue.run_job_task") as mock_task, pytest.raises(DelegationError, match=message):
            await _delegate(run, targets)

        mock_task.aenqueue.assert_not_called()
        assert not await Run.objects.filter(trigger_type=SessionOrigin.DELEGATED_JOB).aexists()

    async def test_a_tool_call_outside_a_bound_run_cannot_delegate(self, admin_user):
        coordinator = await sync_to_async(make_coordinator)(admin_user)

        with pytest.raises(DelegationError, match="no session"):
            await _delegate(None, [_target()], thread_id=coordinator.pk)

    async def test_legs_may_share_a_base_the_coordinators_own_included_and_each_forks_its_branch(
        self, admin_user, create_db_task_result
    ):
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        run = await sync_to_async(make_delegating_run)(coordinator, admin_user)
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
        coordinator = await sync_to_async(make_coordinator)(admin_user, ref="feat/x")
        run = await sync_to_async(make_delegating_run)(coordinator, admin_user)
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
        coordinator = await sync_to_async(make_coordinator)(admin_user, ref="feat/x")
        run = await sync_to_async(make_delegating_run)(coordinator, admin_user)
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
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        run = await sync_to_async(make_delegating_run)(coordinator, admin_user)
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
        coordinator = await sync_to_async(make_coordinator)(member_user)
        run = await sync_to_async(make_delegating_run)(coordinator, member_user)
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
        coordinator = await sync_to_async(make_coordinator)(member_user)
        run = await sync_to_async(make_delegating_run)(coordinator, member_user)

        with patch("sessions.admission.assert_can_run", side_effect=RepositoryAccessDenied(["acme/api"])):
            result = await _delegate(run, [_target()])

        assert (result.batch_id, result.started) == (None, [])
        assert [f.repo_id for f in result.failed] == ["acme/api"]
        assert not await Run.objects.filter(trigger_type=SessionOrigin.DELEGATED_JOB).aexists()

    async def test_a_leg_that_cannot_be_queued_is_reported_failed(self, admin_user):
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        run = await sync_to_async(make_delegating_run)(coordinator, admin_user)

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
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        run = await sync_to_async(make_delegating_run)(coordinator, admin_user)
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
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        run = await sync_to_async(make_delegating_run)(coordinator, admin_user)
        await Run.objects.filter(pk=run.pk).aupdate(muted=True)
        task = await sync_to_async(create_db_task_result)(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, [task])
            await _delegate(run, [_target()])

        assert await Run.objects.filter(trigger_type=SessionOrigin.DELEGATED_JOB).values_list("muted", flat=True).aget()

    async def test_a_leg_on_the_default_branch_the_coordinator_is_on_waits_and_moves_to_what_it_published(
        self, admin_user, create_db_task_result
    ):
        coordinator = await sync_to_async(make_coordinator)(admin_user, ref="main")
        run = await sync_to_async(make_delegating_run)(coordinator, admin_user)
        tasks = [await sync_to_async(create_db_task_result)(status="READY") for _ in range(2)]

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, tasks)
            result = await _delegate(run, [_target("acme/coord", "main"), _target("acme/coord")])
            assert mock_task.aenqueue.await_count == 1
            await Session.objects.filter(pk=coordinator.pk).aupdate(ref="daiv/coordinator-work")
            await Run.objects.afinish(run, RunStatus.SUCCESSFUL, finished_at=timezone.now())

        assert [(leg.ref, leg.starts_after_this_turn) for leg in result.started] == [("main", True), ("", False)]
        assert mock_task.aenqueue.call_args.kwargs["ref"] == "daiv/coordinator-work"


@pytest.mark.django_db(transaction=True)
async def test_only_a_session_that_was_not_delegated_gets_a_delegator(admin_user):
    coordinator = await sync_to_async(make_coordinator)(admin_user)
    leg = await sync_to_async(make_coordinator)(admin_user, parent=coordinator)

    assert isinstance(await adelegator_for(coordinator.pk), SessionJobDelegator)
    assert await adelegator_for(leg.pk) is None


def _resume_event(coordinator: Session) -> InboxEvent | None:
    return InboxEvent.objects.filter(session=coordinator, kind=EventKind.DELEGATION_FINISHED).first()


@pytest.fixture
def drain():
    with patch.object(Drain, "start_logged", autospec=True) as drain:
        yield drain


@pytest.mark.django_db(transaction=True)
class TestResumeCoordinator:
    def test_once_every_leg_finished_it_posts_one_waking_event_with_the_batch_summary(
        self, admin_user, member_user, create_db_task_result, drain
    ):
        coordinator = make_coordinator(admin_user)
        batch_id = uuid.uuid4()
        done = make_leg(
            coordinator,
            batch_id,
            status=RunStatus.SUCCESSFUL,
            task_result=create_db_task_result(),
            result_summary="Bumped.",
            merge_request_web_url="https://git.example/mr/1",
            user=member_user,
        )
        failed = make_leg(
            coordinator, batch_id, status=RunStatus.FAILED, task_result=create_db_task_result(), repo_id="b",
            user=member_user,
        )  # fmt: skip

        resume_coordinator(failed)

        event = _resume_event(coordinator)
        assert (event.source_key, event.wake, event.ref, event.author_id) == (
            f"batch:{batch_id}", True, "main", member_user.pk,
        )  # fmt: skip
        assert event.payload == {
            "batch_id": str(batch_id),
            "summary": render_batch_summary(batch_id, [done, failed]),
            "succeeded": 1,
            "total": 2,
        }
        assert not Run.objects.filter(session=coordinator).exists()
        assert [call.args[0].thread_id for call in drain.call_args_list] == [coordinator.pk]

    def test_the_inbox_starts_the_coordinators_resume_run(self, admin_user, create_db_task_result):
        coordinator = make_coordinator(admin_user)
        batch_id = uuid.uuid4()
        leg = make_leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            resume_coordinator(leg)

        resume = Run.objects.get(session=coordinator)
        assert (resume.trigger_type, resume.status, resume.task_result_id) == (
            SessionOrigin.DELEGATION_RESUME, RunStatus.READY, task.id,
        )  # fmt: skip
        assert resume.prompt == render_batch_summary(batch_id, [leg])
        assert resume.continuation_of_batch_id is None
        assert mock_task.aenqueue.call_args.kwargs["inbox"] is True

    def test_a_busy_coordinator_gets_it_in_its_next_run(self, admin_user, create_db_task_result, drain):
        coordinator = make_coordinator(admin_user, active_run_id="chat-run")
        batch_id = uuid.uuid4()
        leg = make_leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())

        resume_coordinator(leg)

        next_run = Run.objects.get(pk=_resume_event(coordinator).run_id)
        assert (next_run.status, next_run.trigger_type) == (RunStatus.QUEUED, SessionOrigin.DELEGATION_RESUME)

    def test_it_waits_while_a_leg_is_still_running(self, admin_user, create_db_task_result, drain):
        coordinator = make_coordinator(admin_user)
        batch_id = uuid.uuid4()
        finished = make_leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        make_leg(coordinator, batch_id, status=RunStatus.RUNNING, task_result=create_db_task_result(status="RUNNING"))

        resume_coordinator(finished)

        assert _resume_event(coordinator) is None

    def test_a_batch_resumes_its_coordinator_once(self, admin_user, create_db_task_result, drain):
        coordinator = make_coordinator(admin_user)
        batch_id = uuid.uuid4()
        leg = make_leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())

        resume_coordinator(leg)
        resume_coordinator(leg)

        assert InboxEvent.objects.filter(session=coordinator).count() == 1

    def test_a_batch_whose_legs_never_reached_the_queue_resumes_nothing(self, admin_user, drain):
        coordinator = make_coordinator(admin_user)
        leg = make_leg(coordinator, uuid.uuid4(), status=RunStatus.FAILED)

        resume_coordinator(leg)

        assert _resume_event(coordinator) is None

    def test_a_batch_whose_waiting_legs_never_reached_the_queue_still_resumes(self, admin_user, drain):
        coordinator = make_coordinator(admin_user, ref="feat/x")
        delegating = make_delegating_run(coordinator, admin_user)
        Run.objects.filter(pk=delegating.pk).update(status=RunStatus.SUCCESSFUL)
        leg = make_leg(coordinator, uuid.uuid4(), status=RunStatus.FAILED, waits_for=delegating)

        resume_coordinator(leg)

        assert _resume_event(coordinator).ref == "feat/x"

    @pytest.mark.parametrize(
        "fields", [{"trigger_type": SessionOrigin.API_JOB}, {"batch_id": None}], ids=["not-delegated", "no-batch"]
    )
    def test_only_a_delegated_batch_resumes_anything(self, admin_user, create_db_task_result, drain, fields):
        coordinator = make_coordinator(admin_user)
        leg = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        for name, value in fields.items():
            setattr(leg, name, value)

        resume_coordinator(leg)

        assert not InboxEvent.objects.exists()

    def test_a_batch_whose_coordinator_is_gone_resumes_nothing(self, admin_user, create_db_task_result, drain):
        coordinator = make_coordinator(admin_user)
        leg = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        coordinator.delete()

        resume_coordinator(leg)

        assert not InboxEvent.objects.exists()


@pytest.mark.django_db(transaction=True)
class TestResumeStalledBatches:
    def test_a_finished_batch_left_without_an_event_gets_one(self, admin_user, create_db_task_result, drain):
        coordinator = make_coordinator(admin_user)
        batch_id = uuid.uuid4()
        make_leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())

        assert resume_stalled_batches() == 1
        assert _resume_event(coordinator).source_key == f"batch:{batch_id}"
        assert resume_stalled_batches() == 0

    def test_a_batch_an_older_continuation_resumed_is_left_alone(self, admin_user, create_db_task_result, drain):
        coordinator = make_coordinator(admin_user)
        batch_id = uuid.uuid4()
        make_leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        Run.objects.create(
            session=coordinator, trigger_type=SessionOrigin.DELEGATION_RESUME, repo_id="acme/coord",
            status=RunStatus.SUCCESSFUL, continuation_of_batch_id=batch_id,
        )  # fmt: skip

        assert resume_stalled_batches() == 0
        assert _resume_event(coordinator) is None

    def test_a_leg_left_queued_behind_a_finished_run_is_restacked_and_started(self, admin_user, create_db_task_result):
        coordinator = make_coordinator(admin_user, ref="daiv/published")
        delegating = make_delegating_run(coordinator, admin_user)
        Run.objects.filter(pk=delegating.pk).update(status=RunStatus.SUCCESSFUL)
        leg = make_leg(
            coordinator, uuid.uuid4(), status=RunStatus.QUEUED, ref="main", waits_for=delegating, finished_at=None
        )
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            resume_stalled_batches()

        leg.refresh_from_db()
        assert (leg.status, leg.ref, leg.task_result_id) == (RunStatus.READY, "daiv/published", task.id)

    def test_a_batch_that_finished_before_the_window_is_left_alone(self, admin_user, create_db_task_result, drain):
        coordinator = make_coordinator(admin_user)
        long_ago = timezone.now() - STALLED_BATCH_WINDOW - timedelta(minutes=1)
        make_leg(
            coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, task_result=create_db_task_result(),
            finished_at=long_ago,
        )  # fmt: skip

        assert resume_stalled_batches() == 0

    def test_an_unfinished_batch_is_left_alone(self, admin_user, create_db_task_result, drain):
        coordinator = make_coordinator(admin_user)
        batch_id = uuid.uuid4()
        make_leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())
        make_leg(coordinator, batch_id, status=RunStatus.RUNNING, task_result=create_db_task_result(status="RUNNING"))

        assert resume_stalled_batches() == 0
        assert _resume_event(coordinator) is None

    def test_a_continuation_left_queued_on_a_free_coordinator_is_released(self, admin_user, create_db_task_result):
        coordinator = make_coordinator(admin_user)
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
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        batch_id = uuid.uuid4()
        await sync_to_async(make_leg)(
            coordinator,
            batch_id,
            status=RunStatus.SUCCESSFUL,
            ref="release",
            session_ref="fix/bump-auth",
            result_summary=" Bumped. ",
            merge_request_web_url="https://git.example/mr/1",
        )
        await sync_to_async(make_leg)(
            coordinator,
            batch_id,
            status=RunStatus.FAILED,
            repo_id="acme/web",
            error_message="enqueue_failed: OperationalError: host db-internal:5432",
        )
        await sync_to_async(make_leg)(
            coordinator, batch_id, status=RunStatus.RUNNING, repo_id="acme/ops", finished_at=None
        )

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
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        other = await sync_to_async(make_coordinator)(admin_user)
        batch_ids = [uuid.uuid4() for _ in range(MAX_STATUS_BATCHES + 1)]
        for age, batch_id in enumerate(reversed(batch_ids)):
            await sync_to_async(make_leg)(
                coordinator, batch_id, status=RunStatus.SUCCESSFUL, created_at=timezone.now() - timedelta(hours=age)
            )
        await sync_to_async(make_leg)(other, uuid.uuid4(), status=RunStatus.SUCCESSFUL)

        batches = await SessionJobDelegator(coordinator.pk).astatus(batch_id=None)

        assert [batch.batch_id for batch in batches] == [str(b) for b in reversed(batch_ids[1:])]
        assert all(batch.finished for batch in batches)

    async def test_a_session_that_delegated_nothing_has_no_batches(self, admin_user):
        coordinator = await sync_to_async(make_coordinator)(admin_user)

        assert await SessionJobDelegator(coordinator.pk).astatus(batch_id=None) == []

    @pytest.mark.parametrize("batch_id", ["another-sessions", "not-a-uuid"])
    async def test_it_refuses_a_batch_this_session_did_not_delegate(self, admin_user, batch_id):
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        foreign = uuid.uuid4()
        await sync_to_async(make_leg)(
            await sync_to_async(make_coordinator)(admin_user), foreign, status=RunStatus.SUCCESSFUL
        )

        with pytest.raises(DelegationError):
            await SessionJobDelegator(coordinator.pk).astatus(
                batch_id=str(foreign) if batch_id == "another-sessions" else batch_id
            )


def _running_leg(coordinator: Session, batch_id: uuid.UUID) -> Run:
    task_result = make_task_result(status="RUNNING")
    return make_leg(coordinator, batch_id, status=RunStatus.RUNNING, task_result=task_result, finished_at=None)


def _end_task_as_its_worker_would(leg: Run, *, status: str, **task_fields) -> None:
    DBTaskResult.objects.filter(pk=leg.task_result_id).update(status=status, finished_at=timezone.now(), **task_fields)
    Run.objects.select_related("task_result", "session").get(pk=leg.pk).sync_and_save()


@pytest.mark.django_db(transaction=True)
class TestCancel:
    async def test_it_finishes_a_queued_leg_and_asks_a_running_one_to_stop(self, admin_user):
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        batch_id = uuid.uuid4()
        done = await sync_to_async(make_leg)(
            coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=await sync_to_async(make_task_result)()
        )
        queued = await sync_to_async(make_leg)(coordinator, batch_id, status=RunStatus.QUEUED, finished_at=None)
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
        assert not await InboxEvent.objects.filter(session=coordinator).aexists()

    async def test_once_a_stopping_leg_stops_the_coordinator_is_told_it_was_cancelled(self, admin_user, drain):
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        await sync_to_async(make_delegating_run)(coordinator, admin_user)
        batch_id = uuid.uuid4()
        await sync_to_async(make_leg)(coordinator, batch_id, status=RunStatus.SUCCESSFUL)
        running = await sync_to_async(_running_leg)(coordinator, batch_id)

        with patch("sessions.queue.run_job_task"):
            await SessionJobDelegator(coordinator.pk).acancel(batch_id=str(batch_id), thread_ids=[running.session_id])
            await sync_to_async(_end_task_as_its_worker_would)(
                running, status="FAILED", exception_class_path="sessions.executor.run.RunStoppedError"
            )

        await running.arefresh_from_db()
        assert running.is_cancelled
        event = await InboxEvent.objects.aget(session=coordinator)
        assert (event.kind, event.source_key) == (EventKind.DELEGATION_FINISHED, f"batch:{batch_id}")
        assert "## acme/api: cancelled" in event.payload["summary"]
        assert (event.payload["succeeded"], event.payload["total"]) == (1, 2)

    async def test_a_stopping_leg_that_finishes_first_keeps_its_merge_request(self, admin_user, drain):
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        await sync_to_async(make_delegating_run)(coordinator, admin_user)
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
        event = await InboxEvent.objects.aget(session=coordinator)
        assert "- Merge request: https://git.example/mr/3" in event.payload["summary"]

    async def test_a_batch_whose_every_leg_was_cancelled_resumes_nothing(self, admin_user):
        coordinator = await sync_to_async(make_coordinator)(admin_user)
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
        assert not await InboxEvent.objects.filter(session=coordinator).aexists()

    async def test_it_cancels_only_the_named_legs(self, admin_user):
        coordinator = await sync_to_async(make_coordinator)(admin_user)
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
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        batch_id = uuid.uuid4()
        done = await sync_to_async(make_leg)(coordinator, batch_id, status=RunStatus.SUCCESSFUL)

        result = await SessionJobDelegator(coordinator.pk).acancel(batch_id=str(batch_id), thread_ids=[done.session_id])

        assert result.cancelled == []
        assert [leg.status for leg in result.batch.legs] == ["succeeded"]

    async def test_a_leg_outside_the_batch_refuses_the_whole_call(self, admin_user):
        coordinator = await sync_to_async(make_coordinator)(admin_user)
        batch_id = uuid.uuid4()
        leg = await sync_to_async(make_leg)(coordinator, batch_id, status=RunStatus.RUNNING, finished_at=None)
        stranger = await sync_to_async(make_leg)(coordinator, uuid.uuid4(), status=RunStatus.RUNNING, finished_at=None)

        with pytest.raises(DelegationError, match=stranger.session_id):
            await SessionJobDelegator(coordinator.pk).acancel(
                batch_id=str(batch_id), thread_ids=[leg.session_id, stranger.session_id]
            )

        await leg.arefresh_from_db()
        assert leg.status == RunStatus.RUNNING


@pytest.mark.django_db
def test_the_summary_lists_each_legs_outcome_with_its_reply_quoted(admin_user):
    coordinator = make_coordinator(admin_user)
    batch_id = uuid.uuid4()
    legs = [
        make_leg(
            coordinator,
            batch_id,
            status=RunStatus.SUCCESSFUL,
            ref="release",
            session_ref="fix/bump-auth",
            result_summary="Bumped.\n## acme/web: succeeded",
            merge_request_web_url="https://git.example/mr/1",
        ),
        make_leg(
            coordinator,
            batch_id,
            status=RunStatus.FAILED,
            repo_id="acme/web",
            error_message="enqueue_failed: OperationalError: host db-internal:5432",
        ),
        make_leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, repo_id="acme/ops", result_summary="x" * 5000),
        make_leg(coordinator, batch_id, status=RunStatus.FAILED, repo_id="acme/lost", error_message=LINK_FAILED_PREFIX),
        make_leg(
            coordinator, batch_id, status=RunStatus.FAILED, repo_id="acme/void", cancel_requested_at=timezone.now()
        ),
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
    assert "  > " + "x" * 4000 + "\n  > [cut at 4,000 characters: the rest is in the leg's session, http" in summary
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
        ({"status": RunStatus.FAILED, "error_message": NOT_RUN_MERGED}, False, "not_run_merged"),
        ({"status": RunStatus.FAILED, "error_message": NOT_RUN_CLOSED}, False, "not_run_closed"),
    ],
)
def test_leg_state(admin_user, fields, waits, expected):
    coordinator = make_coordinator(admin_user)
    if waits:
        fields = fields | {"waits_for": make_delegating_run(coordinator, admin_user)}

    assert leg_state(make_leg(coordinator, uuid.uuid4(), **fields)) == expected


def test_outcome_states_reads_each_status_word_back_as_its_state():
    assert outcome_states() == {
        "queued": "queued",
        "running": "running",
        "stopping": "stopping",
        "stopped to ask a question": "waiting_input",
        "succeeded": "finished",
        "failed": "failed",
        "cancelled": "cancelled",
        "started, but DAIV lost track of it": "lost",
        "not run: its merge request was merged": "not_run_merged",
        "not run: its merge request was closed": "not_run_closed",
    }


def _finished_leg(coordinator: Session, **fields) -> Run:
    """A leg of ``coordinator`` that finished on a branch and merge request of its own. SYNC ONLY."""
    fields = {
        "status": RunStatus.SUCCESSFUL,
        "ref": "main",
        "session_ref": "daiv/leg-branch",
        "merge_request_iid": 4,
        "merge_request_web_url": "https://git.example/mr/4",
    } | fields
    return make_leg(coordinator, uuid.uuid4(), **fields)


def _follow_up(leg: Run, *, override: dict | None = None) -> DelegatedTarget:
    fields = {"repo_id": leg.repo_id, "ref": "", "prompt": "Make the failing test pass.", "thread_id": leg.session_id}
    return DelegatedTarget(**(fields | (override or {})))


async def _coordinated(user, **leg_fields) -> tuple[Session, Run, Run]:
    coordinator = await sync_to_async(make_coordinator)(user)
    run = await sync_to_async(make_delegating_run)(coordinator, user)
    leg = await sync_to_async(_finished_leg)(coordinator, **leg_fields)
    return coordinator, run, leg


@pytest.mark.django_db(transaction=True)
class TestLegFollowUp:
    async def test_it_continues_the_leg_in_its_own_session_on_its_branch(self, admin_user, create_db_task_result):
        _, run, leg = await _coordinated(admin_user)
        task = await sync_to_async(create_db_task_result)(status="READY")
        sessions = await Session.objects.acount()

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, [task])
            result = await _delegate(run, [_follow_up(leg)])

        assert await Session.objects.acount() == sessions
        follow_up = await Run.objects.exclude(pk=leg.pk).aget(session=leg.session_id)
        assert (str(follow_up.batch_id), follow_up.user_id, follow_up.muted) == (result.batch_id, admin_user.pk, False)
        assert (follow_up.status, follow_up.prompt) == (RunStatus.READY, "Make the failing test pass.")
        event = await InboxEvent.objects.aget(run=follow_up)
        assert (event.kind, event.payload["batch_id"]) == (EventKind.LEG_FOLLOW_UP, result.batch_id)
        [started] = result.started
        assert (started.repo_id, started.ref, started.thread_id) == ("acme/api", "daiv/leg-branch", leg.session_id)
        assert started.session_url.endswith(f"/sessions/{leg.session_id}/")
        assert (started.leg_follow_up, started.starts_after_this_turn) == (True, False)
        assert result.failed == []
        assert mock_task.aenqueue.call_args.kwargs["fork_branch"] is False

    async def test_a_new_leg_and_a_follow_up_share_one_batch(self, admin_user, create_db_task_result):
        _, run, leg = await _coordinated(admin_user)
        tasks = [await sync_to_async(create_db_task_result)(status="READY") for _ in range(2)]

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, tasks)
            result = await _delegate(run, [_target("acme/web"), _follow_up(leg)])

        assert [(leg.repo_id, leg.leg_follow_up) for leg in result.started] == [("acme/web", False), ("acme/api", True)]
        batch = [
            str(batch_id) async for batch_id in Run.objects.by_batch(result.batch_id).values_list("batch_id", flat=True)
        ]
        assert batch == [result.batch_id] * 2

    async def test_a_call_with_only_follow_ups_submits_no_batch(self, admin_user):
        _, run, leg = await _coordinated(admin_user)

        with (
            patch("sessions.delegation.asubmit_batch_runs", new=AsyncMock()) as submitted,
            patch.object(Drain, "start_logged", autospec=True),
        ):
            result = await _delegate(run, [_follow_up(leg)])

        submitted.assert_not_awaited()
        assert result.batch_id is not None

    async def test_its_run_exists_before_the_new_legs_and_its_leg_drains_after_them(self, admin_user):
        _, run, leg = await _coordinated(admin_user)
        order = []

        async def submit(**kwargs):
            follow_up = await Run.objects.exclude(pk=leg.pk).aget(session=leg.session_id)
            order.append(("submit", follow_up.status, kwargs["batch_id"] == follow_up.batch_id))
            return MagicMock(
                outcomes=[MagicMock(spec=Run, repo_id="acme/web", ref="", session_id="s", waits_for_id=None)]
            )

        with (
            patch("sessions.delegation.asubmit_batch_runs", new=AsyncMock(side_effect=submit)),
            patch.object(Drain, "start_logged", autospec=True, side_effect=lambda drain: order.append(drain.thread_id)),
        ):
            await _delegate(run, [_target("acme/web"), _follow_up(leg)])

        assert order == [("submit", RunStatus.QUEUED, True), leg.session_id]

    async def test_only_the_new_legs_repositories_are_checked_when_sent(self, admin_user, create_db_task_result):
        _, run, leg = await _coordinated(admin_user)

        with (
            patch("sessions.delegation.aassert_repo_access", new=AsyncMock()) as checked,
            patch("sessions.delegation.asubmit_batch_runs", new=AsyncMock(return_value=MagicMock(outcomes=[]))),
            patch("sessions.delegation._started_legs", return_value=[StartedLeg("acme/web", "", "s", "u")]),
            patch.object(Drain, "start_logged", autospec=True),
        ):
            await _delegate(run, [_target("acme/web"), _follow_up(leg)])

        checked.assert_awaited_once_with(admin_user, ["acme/web"])

    async def test_a_session_that_is_not_its_leg_is_refused(self, admin_user):
        coordinator, run, leg = await _coordinated(admin_user)
        others = await sync_to_async(_finished_leg)(await sync_to_async(make_coordinator)(admin_user))
        stranger = str(uuid.uuid4())

        result = await _delegate(
            run, [_follow_up(others), DelegatedTarget(repo_id="acme/api", ref="", prompt="p", thread_id=stranger)]
        )

        assert (result.batch_id, result.started) == (None, [])
        assert [(failed.thread_id, failed.error) for failed in result.failed] == [
            (others.session_id, _NOT_A_LEG.format(thread_id=others.session_id)),
            (stranger, _NOT_A_LEG.format(thread_id=stranger)),
        ]
        assert "is not a leg you delegated: give a `thread_id` from a resume" in result.failed[0].error
        assert not await InboxEvent.objects.filter(kind=EventKind.LEG_FOLLOW_UP).aexists()

    async def test_an_unexpected_failure_posting_it_is_logged_and_reported_without_its_text(self, admin_user, caplog):
        _, run, leg = await _coordinated(admin_user)

        with patch("sessions.delegation.LegFollowUps.apost", new=AsyncMock(side_effect=RuntimeError("db-internal"))):
            result = await _delegate(run, [_follow_up(leg)])

        assert [(failed.thread_id, failed.error) for failed in result.failed] == [(leg.session_id, _UNSTARTED_LEG)]
        assert any(record.levelname == "ERROR" and record.exc_info for record in caplog.records)

    async def test_follow_ups_count_toward_the_target_limit(self, admin_user):
        _, run, leg = await _coordinated(admin_user)
        targets = [_target(f"acme/r{i}") for i in range(MAX_DELEGATED_TARGETS)] + [_follow_up(leg)]

        with pytest.raises(DelegationError, match="At most"):
            await _delegate(run, targets)

        assert not await InboxEvent.objects.filter(kind=EventKind.LEG_FOLLOW_UP).aexists()

    async def test_the_same_leg_twice_refuses_the_whole_call(self, admin_user):
        _, run, leg = await _coordinated(admin_user)

        with pytest.raises(DelegationError, match=f"Name each leg once per call: {leg.session_id} appears more than"):
            await _delegate(run, [_follow_up(leg), _target("acme/web"), _follow_up(leg)])

        assert not await InboxEvent.objects.filter(kind=EventKind.LEG_FOLLOW_UP).aexists()
        assert await Run.objects.filter(trigger_type=SessionOrigin.DELEGATED_JOB).acount() == 1


@pytest.mark.django_db(transaction=True)
class TestLegFollowUpRefusals:
    async def _refusal(self, run: Run, target: DelegatedTarget) -> str:
        with patch.object(Drain, "start_logged", autospec=True):
            result = await _delegate(run, [target])
        [failed] = result.failed
        assert failed.thread_id == target.thread_id
        assert not await InboxEvent.objects.filter(kind=EventKind.LEG_FOLLOW_UP).aexists()
        return failed.error

    async def test_it_names_no_branch(self, admin_user):
        _, run, leg = await _coordinated(admin_user)

        assert await self._refusal(run, _follow_up(leg, override={"ref": "release"})) == (
            "A leg follow-up works on the leg's own branch: omit `ref`."
        )

    async def test_it_names_the_legs_repository(self, admin_user):
        _, run, leg = await _coordinated(admin_user)

        assert await self._refusal(run, _follow_up(leg, override={"repo_id": "acme/web"})) == (
            "This leg works on `acme/api`: give that `repo_id`, or delegate a new leg for `acme/web`."
        )

    async def test_the_person_it_acts_for_archived_it(self, admin_user):
        _, run, leg = await _coordinated(admin_user)
        await HiddenSession.objects.acreate(session_id=leg.session_id, user=admin_user)

        assert "archived for the person you act for" in await self._refusal(run, _follow_up(leg))

    async def test_another_persons_archive_does_not_refuse_it(self, admin_user, member_user):
        _, run, leg = await _coordinated(admin_user)
        await HiddenSession.objects.acreate(session_id=leg.session_id, user=member_user)

        with patch.object(Drain, "start_logged", autospec=True):
            result = await _delegate(run, [_follow_up(leg)])

        assert [started.thread_id for started in result.started] == [leg.session_id]

    @pytest.mark.parametrize("busy", ["running", "follow_up_waiting", "waits_for_coordinator"])
    async def test_a_leg_still_working(self, admin_user, busy):
        coordinator, run, leg = await _coordinated(admin_user)
        if busy == "running":
            await Run.objects.filter(pk=leg.pk).aupdate(status=RunStatus.RUNNING)
        elif busy == "follow_up_waiting":
            await sync_to_async(post_leg_follow_up)(await Session.objects.aget(pk=leg.session_id), admin_user)
        else:
            await Run.objects.filter(pk=leg.pk).aupdate(status=RunStatus.QUEUED, waits_for=run)

        error = await self._refusal(run, _follow_up(leg)) if busy != "follow_up_waiting" else None
        if busy == "follow_up_waiting":
            with patch.object(Drain, "start_logged", autospec=True):
                [failed] = (await _delegate(run, [_follow_up(leg)])).failed
            error = failed.error

        assert error == "This leg is still working: you are resumed when its batch finishes, and can follow it up then."

    async def test_a_leg_daiv_lost_track_of(self, admin_user):
        _, run, leg = await _coordinated(admin_user, status=RunStatus.FAILED, error_message=f"{LINK_FAILED_PREFIX}: x")

        error = await self._refusal(run, _follow_up(leg))

        assert error.startswith("DAIV lost track of this leg's last run, which may still be working: check its session")
        assert f"/sessions/{leg.session_id}/" in error

    @pytest.mark.parametrize(("merged", "word"), [(True, "merged"), (False, "closed")])
    async def test_a_merged_or_closed_merge_request(self, admin_user, merged, word):
        _, run, leg = await _coordinated(admin_user)
        await MergeRequestOwnership.objects.acreate(
            repo_id="acme/api", merge_request_iid=4, session_id=leg.session_id, source_branch="daiv/leg-branch"
        )
        await Ownerships("acme/api").aclose(4, merged=merged, by_username="sfr", target_branch="main")

        assert await self._refusal(run, _follow_up(leg)) == (
            f"This leg's merge request was {word}: delegate a new leg for further work."
        )

    async def test_the_earliest_failing_check_gives_the_reason(self, admin_user):
        _, run, leg = await _coordinated(admin_user, status=RunStatus.RUNNING)

        error = await self._refusal(run, _follow_up(leg, override={"ref": "release", "repo_id": "acme/web"}))

        assert error == "A leg follow-up works on the leg's own branch: omit `ref`."

    async def test_a_refused_follow_up_beside_a_valid_one_leaves_the_valid_one_started(self, admin_user):
        coordinator, run, leg = await _coordinated(admin_user)
        other = await sync_to_async(_finished_leg)(coordinator, repo_id="acme/web")

        with patch.object(Drain, "start_logged", autospec=True):
            result = await _delegate(run, [_follow_up(leg, override={"ref": "x"}), _follow_up(other)])

        assert [started.thread_id for started in result.started] == [other.session_id]
        assert [failed.thread_id for failed in result.failed] == [leg.session_id]

    @pytest.mark.parametrize(
        ("reason", "text"),
        [(BUSY, "This leg is still working"), (MergeRequestState.MERGED, "This leg's merge request was merged")],
    )
    async def test_a_refusal_raced_under_the_legs_lock_reads_as_its_check(self, admin_user, reason, text):
        _, run, leg = await _coordinated(admin_user)

        with patch("sessions.delegation.LegFollowUps.apost", new=AsyncMock(side_effect=LegFollowUpRefused(reason))):
            [failed] = (await _delegate(run, [_follow_up(leg)])).failed

        assert failed.error.startswith(text)


@pytest.mark.django_db(transaction=True)
class TestLegFollowUpBatch:
    def _batch(self, admin_user, *, new_leg_status=RunStatus.SUCCESSFUL) -> tuple[Session, Run, Run, uuid.UUID]:
        """A batch of a finished new leg and a leg follow-up waiting on an earlier leg. SYNC ONLY."""
        coordinator = make_coordinator(admin_user)
        earlier = _finished_leg(coordinator)
        batch_id = uuid.uuid4()
        new_leg = make_leg(
            coordinator, batch_id, status=new_leg_status, repo_id="acme/web", task_result=make_task_result()
        )
        follow_up = post_leg_follow_up(earlier.session, admin_user, batch_id=batch_id)
        return coordinator, new_leg, follow_up, batch_id

    def test_the_coordinator_is_resumed_once_after_the_follow_up_ends(self, admin_user, drain):
        coordinator, _, follow_up, _ = self._batch(admin_user)

        resume_coordinator(Run.objects.get(trigger_type=SessionOrigin.DELEGATED_JOB, repo_id="acme/web"))
        assert _resume_event(coordinator) is None

        Run.objects.finish(follow_up, RunStatus.SUCCESSFUL, finished_at=timezone.now())
        resume_coordinator(follow_up)

        assert InboxEvent.objects.filter(session=coordinator, kind=EventKind.DELEGATION_FINISHED).count() == 1
        assert "(leg follow-up)" in _resume_event(coordinator).payload["summary"]

    def test_a_follow_up_refused_access_at_its_start_still_resumes_the_coordinator(self, admin_user):
        coordinator = make_coordinator(admin_user)
        earlier = _finished_leg(coordinator)
        post_leg_follow_up(earlier.session, admin_user, batch_id=uuid.uuid4())

        with patch("sessions.admission.assert_can_run", side_effect=RepositoryAccessDenied(["acme/api"])):
            Drain(earlier.session_id).start()

        summary = _resume_event(coordinator).payload["summary"]
        assert "## acme/api on main: failed (leg follow-up)" in summary
        assert "- Reply: none; open its session to see what happened." in summary

    def test_a_batch_of_follow_ups_all_cancelled_before_starting_resumes_nothing(self, admin_user, drain):
        coordinator = make_coordinator(admin_user)
        earlier = _finished_leg(coordinator)
        follow_up = post_leg_follow_up(earlier.session, admin_user)
        ci = make_event(earlier.session, EventKind.CI_FAILED, ref="daiv/leg-branch")

        result = SessionJobDelegator(coordinator.pk)._cancel(str(follow_up.batch_id), set())

        assert (result.cancelled, result.resumes) == ([earlier.session_id], False)
        assert InboxEvent.objects.get(run=follow_up).state == EventState.DISMISSED
        ci.refresh_from_db()
        assert (ci.state, ci.wake) == (EventState.PENDING, True)
        assert _resume_event(coordinator) is None

    def test_a_running_follow_up_is_stopping_once_cancelled(self, admin_user, drain):
        coordinator = make_coordinator(admin_user)
        earlier = _finished_leg(coordinator)
        follow_up = post_leg_follow_up(earlier.session, admin_user)
        Run.objects.filter(pk=follow_up.pk).update(status=RunStatus.RUNNING)

        result = SessionJobDelegator(coordinator.pk)._cancel(str(follow_up.batch_id), {earlier.session_id})

        assert Run.objects.get(pk=follow_up.pk).cancel_requested_at is not None
        assert [leg.status for leg in result.batch.legs] == ["stopping"]

    def test_its_status_names_where_the_leg_started_its_branch_and_merge_request(self, admin_user):
        coordinator = make_coordinator(admin_user)
        earlier = _finished_leg(coordinator)
        follow_up = post_leg_follow_up(earlier.session, admin_user)
        Run.objects.filter(pk=follow_up.pk).update(status=RunStatus.SUCCESSFUL, ref="daiv/leg-branch")

        [status] = SessionJobDelegator(coordinator.pk)._status(str(follow_up.batch_id))

        [leg] = status.legs
        assert (leg.ref, leg.branch, leg.merge_request_url) == ("main", "daiv/leg-branch", "https://git.example/mr/4")
        assert (leg.thread_id, leg.leg_follow_up) == (earlier.session_id, True)

    @pytest.mark.parametrize(("label", "word"), [(NOT_RUN_MERGED, "merged"), (NOT_RUN_CLOSED, "closed")])
    def test_a_follow_up_not_run_says_why_and_to_delegate_a_new_leg(self, admin_user, label, word):
        coordinator = make_coordinator(admin_user)
        batch_id = uuid.uuid4()
        done = make_leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=make_task_result())
        not_run = make_leg(coordinator, batch_id, status=RunStatus.FAILED, repo_id="acme/web", error_message=label)

        summary = render_batch_summary(batch_id, [done, not_run])

        assert "1 of 2 succeeded" in summary
        section = summary.split("## acme/web: ")[1].split("\n\n")[0]
        assert section.startswith(f"not run: its merge request was {word}\n")
        assert "- Next: delegate a new leg for further work." in section
        assert "Reply" not in section
        resume_coordinator(not_run)
        assert _resume_event(coordinator) is not None


@pytest.mark.django_db
class TestLegReplies:
    def _leg(self, admin_user, reply: str, *, batch_id: uuid.UUID | None = None) -> Run:
        coordinator = make_coordinator(admin_user)
        return make_leg(
            coordinator,
            batch_id or uuid.uuid4(),
            status=RunStatus.SUCCESSFUL,
            task_result=make_task_result(return_value={"response": reply}),
            result_summary=reply[:2000],
        )

    @pytest.mark.parametrize("length", [2600, 4000])
    def test_a_reply_up_to_the_limit_reaches_the_coordinator_whole(self, admin_user, length):
        reply = "f" * (length - 7) + "FINDING"
        leg = self._leg(admin_user, reply)

        summary = render_batch_summary(leg.batch_id, [leg])
        [status] = SessionJobDelegator(leg.session.parent_id)._status(str(leg.batch_id))

        assert "FINDING" in summary
        assert status.legs[0].reply == reply

    def test_a_longer_reply_is_cut_with_a_pointer_to_the_legs_session(self, admin_user):
        leg = self._leg(admin_user, "r" * 4500)

        [status] = SessionJobDelegator(leg.session.parent_id)._status(str(leg.batch_id))

        reply = status.legs[0].reply
        assert reply.startswith("r" * 4000 + "\n[cut at 4,000 characters: the rest is in the leg's session, http")
        assert reply.endswith(f"/sessions/{leg.session_id}/]")
        assert "r" * 4001 not in reply

    def test_the_overview_keeps_the_shorter_limit(self, admin_user):
        leg = self._leg(admin_user, "r" * 2600)

        [status] = SessionJobDelegator(leg.session.parent_id)._status(None)

        assert status.legs[0].reply.startswith("r" * 1000 + "\n[cut at 1,000 characters: the rest is in the leg's")

    def test_a_run_with_no_task_result_falls_back_to_its_summary(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg = make_leg(coordinator, uuid.uuid4(), status=RunStatus.SUCCESSFUL, result_summary="Bumped.")

        [status] = SessionJobDelegator(coordinator.pk)._status(str(leg.batch_id))

        assert status.legs[0].reply == "Bumped."


@pytest.mark.django_db
class TestAddressingLegs:
    def test_each_section_names_the_legs_session_id_and_the_closing_guidance(self, admin_user):
        coordinator = make_coordinator(admin_user)
        leg = _finished_leg(coordinator)

        summary = render_batch_summary(leg.batch_id, [leg])

        assert "- Session: http" in summary
        assert f"- Session id: {leg.session_id}\n" in summary
        assert (
            "Check each leg's outcome and report it where the request came from. A leg's work is on its branch: "
            "merge that branch into yours if it belongs in this change, or leave its merge request for review. To fix "
            "or extend a leg's own work, follow it up: call `delegate_jobs` with a target that has its `thread_id`, "
            "and it continues in its own session, branch and merge request. Delegate a new leg only for separate "
            "work. Either way you are resumed again."
        ) in summary

    def test_the_page_payload_marks_a_follow_up_and_names_a_visible_session(self, admin_user, member_user):
        coordinator = make_coordinator(admin_user)
        earlier = _finished_leg(coordinator)
        follow_up = post_leg_follow_up(earlier.session, admin_user)

        seen = load_delegations(coordinator.pk, admin_user)
        hidden = load_delegations(coordinator.pk, member_user)

        assert [leg["leg_follow_up"] for leg in seen.legs[follow_up.batch_id]] == [True]
        assert seen.legs[follow_up.batch_id][0]["thread_id"] == earlier.session_id
        assert hidden.legs[follow_up.batch_id][0]["thread_id"] == ""
        assert [leg["leg_follow_up"] for leg in seen.legs[earlier.batch_id]] == [False]
