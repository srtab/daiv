import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.delegation import SessionJobDelegator, render_batch_summary, resume_coordinator
from sessions.models import Run, RunStatus, Session, SessionOrigin

from automation.agent.delegation import MAX_DELEGATED_TARGETS, DelegatedTarget, DelegationError
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied
from codebase.conf import settings as codebase_settings
from codebase.models import RepositoryCatalog


def _coordinator(user=None, **fields) -> Session:
    defaults = {"origin": SessionOrigin.API_JOB, "repo_id": "acme/coord", "ref": "main", "user": user}
    return Session.objects.create(thread_id=str(uuid.uuid4()), **(defaults | fields))


def _leg(coordinator: Session, batch_id: uuid.UUID, *, status: str, task_result=None, **fields) -> Run:
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.DELEGATED_JOB,
        repo_id=fields.get("repo_id", "acme/api"),
        parent=coordinator,
    )
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


def _mock_enqueue(mock_task, task_results):
    mock_task.aenqueue = AsyncMock(side_effect=[MagicMock(id=task.id) for task in task_results])


@pytest.mark.django_db(transaction=True)
class TestSessionJobDelegator:
    async def test_it_starts_one_unattended_leg_per_target_under_the_coordinator(
        self, admin_user, create_db_task_result
    ):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        tasks = [await sync_to_async(create_db_task_result)(status="READY") for _ in range(2)]

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, tasks)
            result = await SessionJobDelegator().adelegate(
                thread_id=coordinator.pk,
                goal="Bump auth",
                targets=[_target("acme/api", prompt="Bump it."), _target("acme/web", "release", "Rebuild.")],
            )

        legs = [run async for run in Run.objects.select_related("session").order_by("repo_id")]
        assert [(leg.repo_id, leg.ref, leg.prompt, leg.trigger_type) for leg in legs] == [
            ("acme/api", "", "Bump it.", SessionOrigin.DELEGATED_JOB),
            ("acme/web", "release", "Rebuild.", SessionOrigin.DELEGATED_JOB),
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

    @pytest.mark.parametrize(
        ("session_fields", "targets", "message"),
        [
            ({"with_user": False}, [_target()], "has none"),
            ({"delegated": True}, [_target()], "cannot delegate further"),
            ({}, [], "at least one target"),
            ({}, [_target(f"acme/r{i}") for i in range(MAX_DELEGATED_TARGETS + 1)], "At most"),
            ({}, [_target(prompt="")], "a prompt of its own"),
            ({}, [_target("acme/coord")], "own checkout"),
            ({}, [_target("acme/coord", "main")], "own checkout"),
            ({}, [_target("acme/api"), _target("acme/api", "main")], "listed twice"),
        ],
        ids=[
            "no-user",
            "delegated-session",
            "no-targets",
            "too-many-targets",
            "blank-prompt",
            "own-checkout-by-default-branch",
            "own-checkout-by-name",
            "same-checkout-twice",
        ],
    )
    async def test_a_refused_call_starts_nothing(self, admin_user, session_fields, targets, message):
        await RepositoryCatalog.objects.abulk_create([
            RepositoryCatalog(
                provider=codebase_settings.CLIENT.value, slug=slug, default_branch="main", synced_at=timezone.now()
            )
            for slug in ("acme/coord", "acme/api")
        ])
        user = admin_user if session_fields.get("with_user", True) else None
        coordinator = await sync_to_async(_coordinator)(user)
        if session_fields.get("delegated"):
            parent = await sync_to_async(_coordinator)(admin_user)
            await Session.objects.filter(pk=coordinator.pk).aupdate(parent=parent)

        with patch("sessions.queue.run_job_task") as mock_task, pytest.raises(DelegationError, match=message):
            await SessionJobDelegator().adelegate(thread_id=coordinator.pk, goal="g", targets=targets)

        mock_task.aenqueue.assert_not_called()
        assert not await Run.objects.aexists()

    async def test_a_session_that_does_not_exist_cannot_delegate(self):
        with pytest.raises(DelegationError, match="no session"):
            await SessionJobDelegator().adelegate(thread_id=str(uuid.uuid4()), goal="g", targets=[_target()])

    async def test_another_branch_of_the_coordinators_repository_is_a_distinct_checkout(
        self, admin_user, create_db_task_result
    ):
        coordinator = await sync_to_async(_coordinator)(admin_user)
        task = await sync_to_async(create_db_task_result)(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            _mock_enqueue(mock_task, [task])
            result = await SessionJobDelegator().adelegate(
                thread_id=coordinator.pk, goal="g", targets=[_target("acme/coord", "release")]
            )

        assert [(leg.repo_id, leg.ref) for leg in result.started] == [("acme/coord", "release")]

    async def test_targets_the_user_cannot_write_to_fail_while_the_rest_start(self, member_user, create_db_task_result):
        coordinator = await sync_to_async(_coordinator)(member_user)
        task = await sync_to_async(create_db_task_result)(status="READY")

        def can_run(user, repo_ids):
            if denied := [repo_id for repo_id in repo_ids if repo_id == "acme/secret"]:
                raise RepositoryAccessDenied(denied)

        with patch("sessions.admission.assert_can_run", side_effect=can_run), patch("sessions.queue.run_job_task") as t:
            _mock_enqueue(t, [task])
            result = await SessionJobDelegator().adelegate(
                thread_id=coordinator.pk, goal="g", targets=[_target("acme/api"), _target("acme/secret")]
            )

        assert [leg.repo_id for leg in result.started] == ["acme/api"]
        assert [(f.repo_id, f.error) for f in result.failed] == [("acme/secret", REPO_ACCESS_DENIED_MESSAGE)]

    async def test_when_every_target_is_denied_no_batch_is_made(self, member_user):
        coordinator = await sync_to_async(_coordinator)(member_user)

        with patch("sessions.admission.assert_can_run", side_effect=RepositoryAccessDenied(["acme/api"])):
            result = await SessionJobDelegator().adelegate(thread_id=coordinator.pk, goal="g", targets=[_target()])

        assert (result.batch_id, result.started) == (None, [])
        assert [f.repo_id for f in result.failed] == ["acme/api"]
        assert not await Run.objects.aexists()

    async def test_a_leg_that_cannot_be_queued_is_reported_failed(self, admin_user):
        coordinator = await sync_to_async(_coordinator)(admin_user)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=RuntimeError("broker down"))
            result = await SessionJobDelegator().adelegate(thread_id=coordinator.pk, goal="g", targets=[_target()])

        assert result.started == []
        assert [f.repo_id for f in result.failed] == ["acme/api"]
        assert "broker down" in result.failed[0].error


@pytest.mark.django_db(transaction=True)
class TestResumeCoordinator:
    def test_once_every_leg_finished_it_queues_one_summarised_run_on_the_coordinator_and_releases_it(
        self, admin_user, create_db_task_result
    ):
        coordinator = _coordinator(admin_user, agent_model="m", agent_thinking_level="high")
        batch_id = uuid.uuid4()
        done = _leg(
            coordinator,
            batch_id,
            status=RunStatus.SUCCESSFUL,
            task_result=create_db_task_result(),
            result_summary="Bumped.",
            merge_request_web_url="https://git.example/mr/1",
        )
        failed = _leg(coordinator, batch_id, status=RunStatus.FAILED, task_result=create_db_task_result(), repo_id="b")
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            resume_coordinator(failed)

        continuation = Run.objects.get(continuation_of_batch_id=batch_id)
        assert continuation.session_id == coordinator.pk
        assert (continuation.trigger_type, continuation.status, continuation.task_result_id) == (
            SessionOrigin.DELEGATED_JOB,
            RunStatus.READY,
            task.id,
        )
        assert (continuation.repo_id, continuation.ref, continuation.user_id) == ("acme/coord", "main", admin_user.pk)
        assert (continuation.agent_model, continuation.agent_thinking_level) == ("m", "high")
        assert continuation.prompt == render_batch_summary(batch_id, [done, failed])
        assert mock_task.aenqueue.call_args.kwargs["thread_id"] == coordinator.pk

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

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=RuntimeError("not the point"))
            resume_coordinator(leg)
            resume_coordinator(leg)

        assert Run.objects.filter(continuation_of_batch_id=batch_id).count() == 1

    def test_a_batch_whose_legs_never_reached_the_queue_resumes_nothing(self, admin_user):
        coordinator = _coordinator(admin_user)
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.FAILED)

        resume_coordinator(leg)

        assert not Run.objects.filter(continuation_of_batch_id=batch_id).exists()

    def test_a_busy_coordinator_keeps_its_continuation_queued(self, admin_user, create_db_task_result):
        coordinator = _coordinator(admin_user)
        Run.objects.create(
            session=coordinator, trigger_type=SessionOrigin.API_JOB, repo_id="acme/coord", status=RunStatus.RUNNING
        )
        batch_id = uuid.uuid4()
        leg = _leg(coordinator, batch_id, status=RunStatus.SUCCESSFUL, task_result=create_db_task_result())

        with patch("sessions.queue.run_job_task") as mock_task:
            resume_coordinator(leg)

        mock_task.aenqueue.assert_not_called()
        assert Run.objects.get(continuation_of_batch_id=batch_id).status == RunStatus.QUEUED

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
            result_summary="Bumped.\n## acme/web: succeeded",
            merge_request_web_url="https://git.example/mr/1",
        ),
        _leg(coordinator, batch_id, status=RunStatus.FAILED, repo_id="acme/web"),
        _leg(coordinator, batch_id, status=RunStatus.FAILED, repo_id="acme/ops", error_message="x" * 5000),
    ]

    summary = render_batch_summary(batch_id, legs)

    assert summary.startswith(f"The sub-jobs you delegated (batch {batch_id}) have finished: 1 of 3 succeeded.")
    assert "## acme/api on release: succeeded" in summary
    assert "- Merge request: https://git.example/mr/1" in summary
    assert "  > Bumped.\n  > ## acme/web: succeeded" in summary
    assert "## acme/web: failed\n" in summary
    assert "failed without an error message" in summary
    assert "  > " + "x" * 1000 + "\n" in summary
    assert f"/sessions/{legs[0].session_id}/" in summary
