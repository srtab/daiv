from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from sandbox_envs.models import SandboxEnvironment, Visibility
from sessions.admission import RepoAccessError
from sessions.models import Run, RunStatus, Session, SessionOrigin, SessionRepository
from sessions.multi_repo import (
    MAX_REPOS_PER_SESSION,
    SharedSessionError,
    ais_multi_repo,
    aload_extra_repos,
    apersist_extra_repo_state,
    asubmit_shared_session_run,
)
from sessions.services import RepoTarget

from codebase.authorization import RepositoryAccessDenied
from tests.unit_tests.sessions.conftest import make_task_result

pytestmark = pytest.mark.django_db(transaction=True)


async def _enqueue_task(*args, **kwargs) -> MagicMock:
    row = await sync_to_async(make_task_result)(status="READY")
    return MagicMock(id=row.id)


@pytest.fixture
def enqueue():
    with patch("sessions.queue.run_job_task") as task, patch("sessions.services.generate_batch_title_task") as title:
        task.aenqueue = AsyncMock(side_effect=_enqueue_task)
        title.aenqueue = AsyncMock()
        yield task.aenqueue


@pytest.fixture
def envs(member_user):
    return [
        SandboxEnvironment.objects.create(
            scope=Visibility.USER, user=member_user, name=name, base_image="alpine:latest"
        )
        for name in ("a", "b")
    ]


async def _submit(user, repos, **kwargs):
    return await asubmit_shared_session_run(
        user=user, prompt="fix", repos=repos, trigger_type=SessionOrigin.API_JOB, **kwargs
    )


async def _new_session(user):
    result = await _submit(user, [RepoTarget("g/app", "dev"), RepoTarget("g/mod"), RepoTarget("g/lib")])
    (run,) = result.runs
    return run.session_id


async def test_new_shared_session_has_one_run_on_main_and_rows_for_the_rest(member_user, enqueue, envs):
    env_a, env_b = envs
    result = await _submit(
        member_user, [RepoTarget("g/app", "dev", str(env_a.id)), RepoTarget("g/mod", "", str(env_b.id))]
    )

    (run,) = result.runs
    assert (run.repo_id, run.ref, str(run.sandbox_environment_id)) == ("g/app", "dev", str(env_a.id))
    rows = [r async for r in SessionRepository.objects.filter(session=run.session_id)]
    assert [(r.position, r.repo_id, r.ref, r.sandbox_environment_id) for r in rows] == [(1, "g/mod", "", env_b.id)]
    assert enqueue.await_count == 1
    assert await ais_multi_repo(run.session_id)


async def test_load_extra_repos_is_ordered_by_position(member_user, enqueue):
    thread_id = await _new_session(member_user)

    assert [r.repo_id for r in await aload_extra_repos(thread_id)] == ["g/mod", "g/lib"]


@pytest.mark.parametrize(
    "ids", [["g/a", "g/a"], [f"g/r{i}" for i in range(MAX_REPOS_PER_SESSION + 1)], []], ids=["dup", "many", "empty"]
)
async def test_rejects_duplicates_too_many_and_empty(member_user, enqueue, ids):
    with pytest.raises(SharedSessionError):
        await _submit(member_user, [RepoTarget(i) for i in ids])

    assert await Run.objects.acount() == 0


async def test_failed_row_creation_leaves_no_run_behind(member_user, enqueue):
    with patch("sessions.multi_repo.SessionRepository.objects.bulk_create", side_effect=RuntimeError("boom")):
        result = await _submit(member_user, [RepoTarget("g/app"), RepoTarget("g/mod")])

    assert result.runs == []
    assert "RunCreationFailed" in result.failed[0].error
    assert await Run.objects.acount() == 0
    assert await Session.objects.acount() == 0


@pytest.mark.parametrize(
    "repos", [[RepoTarget("g/app")], [RepoTarget("g/app"), RepoTarget("g/mod"), RepoTarget("g/lib")]]
)
async def test_continuation_accepts_main_alone_or_identical_list(member_user, enqueue, repos):
    thread_id = await _new_session(member_user)
    await Run.objects.filter(session_id=thread_id).aupdate(status=RunStatus.SUCCESSFUL)

    result = await _submit(member_user, repos, thread_id=thread_id)

    (run,) = result.runs
    assert (run.session_id, run.repo_id, run.ref) == (thread_id, "g/app", "dev")
    assert await SessionRepository.objects.filter(session_id=thread_id).acount() == 2


@pytest.mark.parametrize(
    "repos",
    [
        [RepoTarget("g/app"), RepoTarget("g/lib"), RepoTarget("g/mod")],
        [RepoTarget("g/app"), RepoTarget("g/mod")],
        [RepoTarget("g/app"), RepoTarget("g/mod"), RepoTarget("g/lib"), RepoTarget("g/new")],
        [RepoTarget("g/other")],
        [RepoTarget("g/app", "other")],
    ],
)
async def test_continuation_rejects_other_lists_and_refs(member_user, enqueue, repos):
    thread_id = await _new_session(member_user)

    with pytest.raises(SharedSessionError):
        await _submit(member_user, repos, thread_id=thread_id)

    assert await Run.objects.filter(session_id=thread_id).acount() == 1
    assert await SessionRepository.objects.filter(session_id=thread_id).acount() == 2


async def test_continuation_queues_behind_an_active_run(member_user, enqueue):
    thread_id = await _new_session(member_user)
    enqueue.reset_mock()

    result = await _submit(member_user, [RepoTarget("g/app")], thread_id=thread_id)

    assert result.runs[0].status == RunStatus.QUEUED
    enqueue.assert_not_awaited()


async def test_shared_flag_on_single_repo_thread_is_rejected(member_user, enqueue):
    thread_id = str(uuid.uuid4())
    await Session.objects.acreate(thread_id=thread_id, origin=SessionOrigin.API_JOB, repo_id="g/app")

    with pytest.raises(SharedSessionError, match="is not a multi-repository session"):
        await _submit(member_user, [RepoTarget("g/app")], thread_id=thread_id)


async def test_unknown_thread_is_rejected(member_user, enqueue):
    with pytest.raises(SharedSessionError, match="is not a multi-repository session"):
        await _submit(member_user, [RepoTarget("g/app")], thread_id=str(uuid.uuid4()))


async def test_access_checked_for_every_repo(member_user, enqueue):
    with (
        patch("sessions.admission.assert_can_run", side_effect=RepositoryAccessDenied(["g/mod"])) as check,
        pytest.raises(RepoAccessError),
    ):
        await _submit(member_user, [RepoTarget("g/app"), RepoTarget("g/mod")])

    assert list(check.call_args.args[1]) == ["g/app", "g/mod"]
    assert await Run.objects.acount() == 0


async def test_enqueue_failure_is_reported_as_failed(member_user):
    with patch("sessions.queue.run_job_task") as task:
        task.aenqueue = AsyncMock(side_effect=RuntimeError("down"))
        result = await _submit(member_user, [RepoTarget("g/app"), RepoTarget("g/mod")])

    assert result.runs == []
    assert result.failed[0].repo_id == "g/app"


def _publish(repo_id, *, iid=None, branch="", published=False, current_ref="", error=None):
    from automation.agent.publishers import RepoPublish
    from codebase.base import MergeRequest, User

    mr = (
        MergeRequest(
            repo_id=repo_id,
            merge_request_id=iid,
            source_branch=branch,
            target_branch="main",
            title="t",
            description="d",
            web_url=f"https://git/{repo_id}/-/merge_requests/{iid}",
            author=User(id=1, username="bot"),
        )
        if iid
        else None
    )
    return RepoPublish(repo_id=repo_id, merge_request=mr, published=published, current_ref=current_ref, error=error)


async def _session_with_rows(*repo_ids):
    session = await Session.objects.acreate(thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="g/main")
    for position, repo_id in enumerate(repo_ids, start=1):
        await SessionRepository.objects.acreate(session=session, position=position, repo_id=repo_id)
    return session


async def test_persist_extra_repo_state_keeps_each_branch_and_mr():
    session = await _session_with_rows("g/mod", "g/lib", "g/err")

    await apersist_extra_repo_state(
        session.thread_id,
        [
            _publish("g/mod", iid=7, branch="daiv/mod", published=True),
            _publish("g/lib", iid=8, branch="daiv/lib", published=True),
            _publish("g/err", error="boom", current_ref="daiv/err"),
            _publish("g/unknown", iid=9, branch="daiv/x", published=True),
        ],
    )

    rows = {row.repo_id: row for row in await aload_extra_repos(session.thread_id)}
    assert (rows["g/mod"].ref, rows["g/mod"].merge_request_iid) == ("daiv/mod", 7)
    assert rows["g/mod"].merge_request_web_url == "https://git/g/mod/-/merge_requests/7"
    assert (rows["g/lib"].ref, rows["g/lib"].merge_request_iid) == ("daiv/lib", 8)
    assert (rows["g/err"].ref, rows["g/err"].merge_request_iid) == ("", None)


async def test_persist_extra_repo_state_without_an_mr():
    session = await _session_with_rows("g/pushed", "g/local", "g/detached")

    await apersist_extra_repo_state(
        session.thread_id,
        [
            _publish("g/pushed", published=True, current_ref="daiv/pushed"),
            _publish("g/local", published=False, current_ref="feat/local"),
            _publish("g/detached", published=False, current_ref=""),
        ],
    )

    refs = {row.repo_id: row.ref for row in await aload_extra_repos(session.thread_id)}
    assert refs == {"g/pushed": "", "g/local": "feat/local", "g/detached": ""}
