import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django_tasks_db.models import DBTaskResult
from mcp_connectors.models import MCPServer
from ninja.testing import TestAsyncClient
from sessions.executor.tasks import run_job_task
from sessions.models import EventKind, InboxEvent, Run, RunStatus, Session, SessionOrigin
from sessions.services import BatchSubmitResult

from accounts.models import APIKey, User
from core.models import Provider, ProviderType
from core.provider_failures import failure_message
from core.site_settings import site_settings
from daiv.api import api
from tests.unit_tests.conftest import SAMPLE_QUESTION_PAYLOAD
from tests.unit_tests.mcp_connectors.helpers import only_servers
from tests.unit_tests.sessions.conftest import FakeRun, fake_acreate_run, make_artifact, make_task_result


@pytest.fixture
def client():
    return TestAsyncClient(api)


@pytest.fixture
def api_key(db):
    user = User.objects.create_user(username="testuser", email="test@test.com", password="testpass")  # noqa: S106
    _, key = async_to_sync(APIKey.objects.create_key)(user=user, name="test-key")
    return key


@pytest.fixture
def authenticated_client(api_key):
    return TestAsyncClient(api, headers={"Authorization": f"Bearer {api_key}"})


@pytest.fixture
def openrouter_provider(db):
    Provider.objects.filter(slug="openrouter").delete()
    return Provider.objects.create(
        slug="openrouter", provider_type=ProviderType.OPENROUTER, api_key="sk-test", is_enabled=True
    )


def _single_repo_body(**overrides):
    body = {"repos": [{"repo_id": "group/project", "ref": None}], "prompt": "p"}
    body.update(overrides)
    return body


async def _make_task_row(task_id=None) -> AsyncMock:
    """Create a real DBTaskResult row so the Run FK is satisfied, and return a mock that
    exposes the row's id."""
    tid = task_id or uuid.uuid4()
    await DBTaskResult.objects.acreate(
        id=tid,
        status="READY",
        task_path=run_job_task.module_path,
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after="9999-01-01T00:00:00Z",
        return_value={},
    )
    m = AsyncMock()
    m.id = tid
    return m


def _patch_acreate_run():
    return patch("sessions.services.acreate_run", new_callable=AsyncMock, side_effect=fake_acreate_run)


# --- Authentication tests ---


@pytest.mark.django_db
async def test_submit_job_unauthenticated(client: TestAsyncClient):
    response = await client.post("/jobs", json=_single_repo_body(prompt="List all Python files"))
    assert response.status_code == 401


@pytest.mark.django_db
async def test_get_job_status_unauthenticated(client: TestAsyncClient):
    response = await client.get(f"/jobs/{uuid.uuid4()}")
    assert response.status_code == 401


# --- Validation tests ---


@pytest.mark.django_db(transaction=True)
async def test_submit_job_missing_prompt(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post("/jobs", json={"repos": [{"repo_id": "group/project", "ref": None}]})
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_empty_repos(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post("/jobs", json={"repos": [], "prompt": "p"})
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_oversized_repos(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": f"o/r{i}", "ref": None} for i in range(21)], "prompt": "p"}
    )
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("repo_id", ["", " / "])
async def test_submit_job_empty_repo_id(authenticated_client: TestAsyncClient, repo_id):
    response = await authenticated_client.post("/jobs", json=_single_repo_body(repos=[{"repo_id": repo_id}]))
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_empty_prompt(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": "group/project", "ref": None}], "prompt": ""}
    )
    assert response.status_code == 422


# --- Submit job tests ---


@pytest.mark.django_db(transaction=True)
async def test_submit_job_success(authenticated_client: TestAsyncClient):
    task_id = uuid.uuid4()

    async def _aenq(**kwargs):
        return await _make_task_row(task_id)

    with patch("sessions.queue.run_job_task") as mock_task, _patch_acreate_run():
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="List all files"))

    assert response.status_code == 202
    data = response.json()
    assert "batch_id" in data
    assert len(data["jobs"]) == 1
    # Verify it's a valid UUID
    assert uuid.UUID(data["jobs"][0]["job_id"])
    assert data["jobs"][0]["thread_id"]
    assert data["jobs"][0]["status"] in ("READY", "QUEUED")
    assert data["failed"] == []
    mock_task.aenqueue.assert_called_once()
    kwargs = mock_task.aenqueue.call_args.kwargs
    assert kwargs["repo_id"] == "group/project"
    assert kwargs["prompt"] == "List all files"
    assert kwargs["ref"] is None
    assert kwargs["agent_model"] is None
    assert kwargs["agent_thinking_level"] is None
    assert "use_max" not in kwargs
    assert kwargs["thread_id"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_multi_repo(authenticated_client: TestAsyncClient):
    async def _aenq(**kwargs):
        return await _make_task_row()

    with patch("sessions.queue.run_job_task") as mock_task:
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post(
            "/jobs", json={"repos": [{"repo_id": "o/a", "ref": None}, {"repo_id": "o/b", "ref": "dev"}], "prompt": "p"}
        )

    assert response.status_code == 202
    data = response.json()
    assert len(data["jobs"]) == 2


@pytest.mark.django_db(transaction=True)
async def test_submit_job_rejects_unknown_provider(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": "group/project"}], "prompt": "Fix it", "agent_model": "bogus:nope"}
    )
    assert response.status_code == 400
    assert "Unknown provider prefix" in response.json()["detail"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_rejects_invalid_thinking_level(authenticated_client: TestAsyncClient):
    """``agent_thinking_level`` outside the enum returns a clear 4xx instead of a silent
    accept. Ninja/Pydantic catches the enum mismatch at the protocol layer (422)."""
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": "group/project"}], "prompt": "Fix it", "agent_thinking_level": "extreme"}
    )
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_rejects_extra_field(authenticated_client: TestAsyncClient):
    """``JobSubmitRequest`` is locked to ``extra='forbid'`` so a stale client still
    sending the dropped ``use_max`` field gets a 422 instead of a silent strip+202."""
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": "group/project"}], "prompt": "Fix it", "use_max": True}
    )
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_forwards_agent_override(authenticated_client: TestAsyncClient, openrouter_provider):
    async def _aenq(**kwargs):
        return await _make_task_row()

    with patch("sessions.queue.run_job_task") as mock_task, _patch_acreate_run() as mock_create:
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post(
            "/jobs",
            json={
                "repos": [{"repo_id": "group/project"}],
                "prompt": "Fix the bug",
                "agent_model": "openrouter:anthropic/claude-haiku-4.5",
                "agent_thinking_level": "low",
            },
        )

    assert response.status_code == 202
    assert mock_create.await_args.kwargs["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert mock_create.await_args.kwargs["agent_thinking_level"] == "low"
    enqueue_kwargs = mock_task.aenqueue.call_args.kwargs
    assert enqueue_kwargs["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert enqueue_kwargs["agent_thinking_level"] == "low"


def _fake_submit_factory(captured: dict):
    """Return an async ``asubmit_batch_runs`` stand-in that records kwargs and returns one run."""
    from sessions.services import BatchSubmitResult

    async def _fake_submit(**kwargs):
        captured.update(kwargs)
        run = FakeRun(task_result_id=None)
        return BatchSubmitResult(batch_id=uuid.uuid4(), outcomes=[run])

    return _fake_submit


@pytest.mark.django_db(transaction=True)
async def test_submit_job_accepts_muted_and_forwards_it(authenticated_client: TestAsyncClient):
    """POST /jobs with muted=True threads ``muted=True`` into ``asubmit_batch_runs``."""
    captured: dict = {}
    with patch("sessions.multi_repo.asubmit_batch_runs", side_effect=_fake_submit_factory(captured)):
        response = await authenticated_client.post("/jobs", json=_single_repo_body(muted=True))
    assert response.status_code == 202
    assert captured.get("muted") is True


@pytest.mark.django_db(transaction=True)
async def test_submit_job_gives_a_repository_its_own_prompt(authenticated_client: TestAsyncClient):
    captured: dict = {}
    body = _single_repo_body(repos=[{"repo_id": "group/project", "prompt": "only here"}], prompt="batch")
    with patch("sessions.multi_repo.asubmit_batch_runs", side_effect=_fake_submit_factory(captured)):
        response = await authenticated_client.post("/jobs", json=body)

    assert response.status_code == 202
    assert captured["prompt"] == "batch"
    assert [(target.repo_id, target.prompt) for target in captured["repos"]] == [("group/project", "only here")]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_strips_slashes_around_the_repo_id(authenticated_client: TestAsyncClient):
    captured: dict = {}
    body = _single_repo_body(repos=[{"repo_id": "/group/project/"}])
    with patch("sessions.multi_repo.asubmit_batch_runs", side_effect=_fake_submit_factory(captured)):
        response = await authenticated_client.post("/jobs", json=body)

    assert response.status_code == 202
    assert [target.repo_id for target in captured["repos"]] == ["group/project"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_forwards_resolved_mcp_overrides(authenticated_client: TestAsyncClient):
    await sync_to_async(only_servers)(("grafana", MCPServer.Status.ON_DEMAND))
    captured: dict = {}
    with patch("sessions.multi_repo.asubmit_batch_runs", side_effect=_fake_submit_factory(captured)):
        response = await authenticated_client.post("/jobs", json=_single_repo_body(mcp_overrides={"grafana": True}))

    assert response.status_code == 202
    assert captured["mcp_overrides"] == {"grafana": "on"}


@pytest.mark.django_db(transaction=True)
async def test_submit_job_refuses_a_blank_per_repository_prompt(authenticated_client: TestAsyncClient):
    body = _single_repo_body(repos=[{"repo_id": "group/project", "prompt": "  "}])

    response = await authenticated_client.post("/jobs", json=body)

    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_reports_each_entry_of_a_repository_listed_twice(authenticated_client: TestAsyncClient):
    from sessions.services import BatchSubmitFailure, BatchSubmitResult

    run = FakeRun(task_result_id=None)
    result = BatchSubmitResult(
        batch_id=uuid.uuid4(), outcomes=[BatchSubmitFailure(repo_id="group/project", ref="", error="boom"), run]
    )
    body = _single_repo_body(
        repos=[{"repo_id": "group/project", "prompt": "first"}, {"repo_id": "group/project", "prompt": "second"}]
    )
    with patch("sessions.multi_repo.asubmit_batch_runs", new=AsyncMock(return_value=result)):
        response = await authenticated_client.post("/jobs", json=body)

    assert response.status_code == 202
    assert [job["job_id"] for job in response.json()["jobs"]] == [str(run.id)]
    assert [failure["error"] for failure in response.json()["failed"]] == ["boom"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_notify_on_rejected(authenticated_client: TestAsyncClient):
    """notify_on is no longer accepted — JobSubmitRequest is extra='forbid'."""
    response = await authenticated_client.post("/jobs", json=_single_repo_body(notify_on="always"))
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_muted_defaults_to_false(authenticated_client: TestAsyncClient):
    """Omitting ``muted`` defaults to False."""
    captured: dict = {}
    with patch("sessions.multi_repo.asubmit_batch_runs", side_effect=_fake_submit_factory(captured)):
        response = await authenticated_client.post("/jobs", json=_single_repo_body())
    assert response.status_code == 202
    assert captured.get("muted") is False


@pytest.mark.django_db(transaction=True)
async def test_submit_job_refuses_with_400_when_no_model_can_be_resolved(authenticated_client: TestAsyncClient):
    with patch.multiple(site_settings, agent_model_name=""), patch("sessions.multi_repo.asubmit_batch_runs") as submit:
        response = await authenticated_client.post("/jobs", json=_single_repo_body())

    assert response.status_code == 400
    assert "no system default is configured" in response.json()["detail"]
    submit.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_submit_job_accepts_an_explicit_model_when_the_site_has_none(authenticated_client: TestAsyncClient):
    captured: dict = {}
    with (
        patch.multiple(site_settings, agent_model_name=""),
        patch("automation.agent.validators.validate_agent_override", return_value=("openrouter:z-ai/glm-5.2", "")),
        patch("sessions.multi_repo.asubmit_batch_runs", side_effect=_fake_submit_factory(captured)),
    ):
        response = await authenticated_client.post(
            "/jobs", json=_single_repo_body(agent_model="openrouter:z-ai/glm-5.2")
        )

    assert response.status_code == 202
    assert captured["agent_model"] == "openrouter:z-ai/glm-5.2"


_SHARED_BODY = {"repos": [{"repo_id": "g/app"}, {"repo_id": "g/mod"}], "prompt": "x"}


def _shared_submit(captured: dict):
    from sessions.services import BatchSubmitResult

    async def _fake(**kwargs):
        captured.update(kwargs)
        return BatchSubmitResult(batch_id=uuid.uuid4(), outcomes=[FakeRun(task_result_id=None)], shared=True)

    return _fake


@pytest.mark.django_db(transaction=True)
async def test_shared_session_returns_one_job_on_the_main_repo(authenticated_client: TestAsyncClient):
    captured: dict = {}
    with patch("sessions.multi_repo._asubmit_shared_session_run", side_effect=_shared_submit(captured)):
        response = await authenticated_client.post("/jobs", json={**_SHARED_BODY, "shared_session": True})

    assert response.status_code == 202
    assert [job["repo_id"] for job in response.json()["jobs"]] == ["g/app"]
    assert [t.repo_id for t in captured["repos"]] == ["g/app", "g/mod"]
    assert captured["thread_id"] is None


@pytest.mark.django_db(transaction=True)
async def test_shared_session_errors_are_400(authenticated_client: TestAsyncClient):
    from sessions.multi_repo import SharedSessionError

    with patch("sessions.multi_repo._asubmit_shared_session_run", side_effect=SharedSessionError("too many")):
        response = await authenticated_client.post("/jobs", json={**_SHARED_BODY, "shared_session": True})

    assert response.status_code == 400
    assert response.json() == {"detail": "too many"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "repos",
    [
        [{"repo_id": f"g/r{i}"} for i in range(6)],
        [{"repo_id": "g/app"}, {"repo_id": "g/app"}],
        [{"repo_id": "g/app"}, {"repo_id": "g/mod", "prompt": "other"}],
    ],
    ids=["too-many", "duplicate", "per-repo-prompt"],
)
async def test_a_bad_shared_session_list_is_refused_before_admission(authenticated_client: TestAsyncClient, repos):
    with patch("sessions.multi_repo.aadmit") as admit:
        response = await authenticated_client.post(
            "/jobs", json={"repos": repos, "prompt": "x", "shared_session": True}
        )

    assert response.status_code == 400
    admit.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_a_shared_continuation_with_a_ref_is_refused_before_admission(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, user=user, repo_id="g/app"
    )

    with (
        patch("sessions.multi_repo.ais_multi_repo", new=AsyncMock(return_value=True)),
        patch("sessions.multi_repo.aadmit") as admit,
    ):
        response = await authenticated_client.post(
            "/jobs", json={"repos": [{"repo_id": "g/app", "ref": "dev"}], "prompt": "x", "thread_id": session.thread_id}
        )

    assert response.status_code == 400
    assert "cannot set a ref" in response.json()["detail"]
    admit.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_continuing_a_multi_repo_thread_needs_no_flag(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, user=user, repo_id="g/app"
    )
    captured: dict = {}
    with (
        patch("sessions.multi_repo.ais_multi_repo", new=AsyncMock(return_value=True)),
        patch("sessions.multi_repo._asubmit_shared_session_run", side_effect=_shared_submit(captured)),
        patch("sessions.multi_repo.asubmit_batch_runs") as batch,
    ):
        response = await authenticated_client.post(
            "/jobs", json={"repos": [{"repo_id": "g/app"}], "prompt": "x", "thread_id": session.thread_id}
        )

    assert response.status_code == 202
    assert captured["thread_id"] == session.thread_id
    batch.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_a_multi_repo_thread_still_requires_ownership(authenticated_client: TestAsyncClient):
    with patch("sessions.multi_repo.ais_multi_repo", new=AsyncMock(return_value=True)):
        response = await authenticated_client.post(
            "/jobs", json={"repos": [{"repo_id": "g/app"}], "prompt": "x", "thread_id": str(uuid.uuid4())}
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "thread_id not found"}


@pytest.mark.django_db(transaction=True)
async def test_a_failed_continuation_of_a_multi_repo_thread_is_reported_as_failed(
    authenticated_client: TestAsyncClient,
):
    from sessions.models import SessionRepository

    user = await User.objects.aget(username="testuser")
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, user=user, repo_id="g/app", ref="daiv/fix-1"
    )
    await SessionRepository.objects.acreate(session=session, position=1, repo_id="g/mod")

    with patch("sessions.queue.run_job_task") as task:
        task.aenqueue = AsyncMock(side_effect=RuntimeError("down"))
        response = await authenticated_client.post(
            "/jobs", json={"repos": [{"repo_id": "g/app"}], "prompt": "x", "thread_id": session.thread_id}
        )

    assert response.status_code == 202
    data = response.json()
    assert data["jobs"] == []
    assert [(f["repo_id"], f["ref"]) for f in data["failed"]] == [("g/app", "")]
    assert data["failed"][0]["error"] == "FollowUpNotStarted: not_started"


@pytest.mark.django_db(transaction=True)
async def test_batch_path_unchanged_without_flag(authenticated_client: TestAsyncClient):
    captured: dict = {}
    with (
        patch("sessions.multi_repo.asubmit_batch_runs", side_effect=_fake_batch_two(captured)),
        patch("sessions.multi_repo._asubmit_shared_session_run") as shared,
    ):
        response = await authenticated_client.post("/jobs", json=_SHARED_BODY)

    assert response.status_code == 202
    assert len(response.json()["jobs"]) == 2
    shared.assert_not_called()


def _fake_batch_two(captured: dict):
    from sessions.services import BatchSubmitResult

    async def _fake(**kwargs):
        captured.update(kwargs)
        runs = [FakeRun(task_result_id=None), FakeRun(task_result_id=None)]
        return BatchSubmitResult(batch_id=uuid.uuid4(), outcomes=runs)

    return _fake


@pytest.mark.django_db(transaction=True)
async def test_submit_job_all_enqueue_failures_reported(authenticated_client: TestAsyncClient):
    with patch("sessions.queue.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(side_effect=Exception("DB down"))
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="List all files"))

    assert response.status_code == 202
    data = response.json()
    assert data["jobs"] == []
    assert len(data["failed"]) == 1
    assert data["failed"][0]["repo_id"] == "group/project"


# --- Get job status tests (Run-based) ---


async def _create_run_row(
    user, status="SUCCESSFUL", result_summary="", merge_request_web_url="", error_message="", question=None
):
    """Create a real Session+Run row for use in get_job_status tests."""
    thread_id = str(uuid.uuid4())
    session = await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.API_JOB, repo_id="group/project", user=user
    )
    return await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.API_JOB,
        repo_id="group/project",
        user=user,
        status=status,
        result_summary=result_summary,
        merge_request_web_url=merge_request_web_url,
        error_message=error_message,
        question=question,
    )


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_successful(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status="SUCCESSFUL", result_summary="Here are the files...")
    response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == str(run.id)
    assert data["status"] == "SUCCESSFUL"
    assert data["result"] == "Here are the files..."
    assert data["error"] is None


@pytest.mark.django_db(transaction=True)
async def test_job_status_lists_every_mr_and_error(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, merge_request_web_url="https://git/g/main/-/merge_requests/1")
    run.merge_requests = [
        {"repo_id": "group/project", "merge_request_id": 1, "web_url": "https://git/g/main/-/merge_requests/1"},
        {"repo_id": "g/mod", "merge_request_id": 7, "web_url": "https://git/g/mod/-/merge_requests/7"},
    ]
    run.publish_errors = [{"repo_id": "g/lib", "error": "Publishing to g/lib failed (X)"}]
    await run.asave()

    data = (await authenticated_client.get(f"/jobs/{run.id}")).json()

    assert data["merge_request_url"] == "https://git/g/main/-/merge_requests/1"
    assert data["merge_requests"] == [
        {"repo_id": "group/project", "url": "https://git/g/main/-/merge_requests/1"},
        {"repo_id": "g/mod", "url": "https://git/g/mod/-/merge_requests/7"},
    ]
    assert data["publish_errors"] == [{"repo_id": "g/lib", "error": "Publishing to g/lib failed (X)"}]


@pytest.mark.django_db(transaction=True)
async def test_job_status_legacy_run_lists_its_single_mr(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, merge_request_web_url="https://git/g/p/-/merge_requests/3")

    data = (await authenticated_client.get(f"/jobs/{run.id}")).json()

    assert data["merge_requests"] == [{"repo_id": "group/project", "url": "https://git/g/p/-/merge_requests/3"}]
    assert data["publish_errors"] == []


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_returns_the_full_response_past_the_summary_cap(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    response_text = "x" * 2500
    run = await _create_run_row(user, status="SUCCESSFUL", result_summary=response_text[:2000])
    run.task_result = await sync_to_async(make_task_result)(return_value={"response": response_text})
    await run.asave(update_fields=["task_result"])

    response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.json()["result"] == response_text


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_waiting_input_carries_the_question(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(
        user, status="WAITING_INPUT", result_summary="**Database** — Which?", question=SAMPLE_QUESTION_PAYLOAD
    )
    response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "WAITING_INPUT"
    assert data["question"] == SAMPLE_QUESTION_PAYLOAD
    assert data["result"] == "**Database** — Which?"
    assert data["error"] is None


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_without_a_question_returns_null(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status="SUCCESSFUL", result_summary="Here are the files...")
    response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.status_code == 200
    assert response.json()["question"] is None


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_failed(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status="FAILED")
    response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "FAILED"
    assert data["result"] is None
    assert data["error"] == "Job execution failed"


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_not_found(authenticated_client: TestAsyncClient):
    response = await authenticated_client.get(f"/jobs/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_invalid_uuid(authenticated_client: TestAsyncClient):
    response = await authenticated_client.get("/jobs/not-a-uuid")
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_other_user_run_returns_404(authenticated_client: TestAsyncClient):
    """Runs belonging to other users must not be accessible."""
    other = await User.objects.acreate_user(username="other2", email="other2@test.com", password="x")  # noqa: S106
    run = await _create_run_row(other, status="SUCCESSFUL", result_summary="secret")
    response = await authenticated_client.get(f"/jobs/{run.id}")
    assert response.status_code == 404


# --- Thread continuation tests ---


@pytest.mark.django_db(transaction=True)
class TestThreadContinuationAPI:
    async def test_response_includes_thread_id_and_status(self, authenticated_client):
        # New thread, no prior Run — should be READY
        with patch("sessions.queue.run_job_task") as mock_task, _patch_acreate_run():
            mock_task.aenqueue = AsyncMock(return_value=await _make_task_row())
            mock_task.module_path = run_job_task.module_path
            response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="x"))
        assert response.status_code == 202
        body = response.json()
        assert body["jobs"][0]["thread_id"]
        assert body["jobs"][0]["status"] == "READY"

    async def test_a_follow_up_whose_run_already_started_is_reported_accepted(self, authenticated_client):
        """The inbox may start a follow-up's run before the submit returns; it is still the job the caller asked for."""
        run = SimpleNamespace(id=uuid.uuid4(), session_id=uuid.uuid4(), status=RunStatus.RUNNING)
        result = BatchSubmitResult(batch_id=uuid.uuid4(), outcomes=[run])
        with patch("sessions.multi_repo.asubmit_batch_runs", new=AsyncMock(return_value=result)):
            response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="x"))

        assert response.status_code == 202
        assert response.json()["jobs"][0] == {
            "job_id": str(run.id),
            "repo_id": "group/project",
            "ref": None,
            "thread_id": str(run.session_id),
            "status": "READY",
        }

    async def test_continuation_with_unknown_thread_id_rejects(self, authenticated_client):
        body = _single_repo_body(prompt="x", thread_id=str(uuid.uuid4()))
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 400
        assert "thread_id not found" in response.json()["detail"]

    async def test_continuation_of_an_archived_session_rejects(self, authenticated_client, db):
        user = await User.objects.aget(username="testuser")
        thread = str(uuid.uuid4())
        await Session.objects.acreate(
            thread_id=thread, origin=SessionOrigin.API_JOB, repo_id="a/b", user=user, archived_at=timezone.now()
        )
        body = _single_repo_body(prompt="x", thread_id=thread)
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 400
        assert response.json()["detail"] == "thread_id is archived; unarchive the session to continue it"

    async def test_continuation_with_other_user_thread_id_rejects(self, authenticated_client, db):
        other = await User.objects.acreate_user(username="other", email="o@t.com", password="x")  # noqa: S106
        thread = str(uuid.uuid4())
        session = await Session.objects.acreate(
            thread_id=thread, origin=SessionOrigin.API_JOB, repo_id="a/b", user=other
        )
        await Run.objects.acreate(session=session, trigger_type=SessionOrigin.API_JOB, repo_id="a/b", user=other)
        body = _single_repo_body(prompt="x", thread_id=thread)
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 400
        assert "thread_id not found" in response.json()["detail"]

    async def test_continuation_with_multi_repo_rejects(self, authenticated_client):
        body = {
            "repos": [{"repo_id": "a/b", "ref": None}, {"repo_id": "c/d", "ref": None}],
            "prompt": "x",
            "thread_id": str(uuid.uuid4()),
        }
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 400
        assert "exactly one repo" in response.json()["detail"]

    async def test_job_id_is_run_id(self, authenticated_client):
        created_runs: list = []

        async def capture_acreate_run(**kwargs):
            run = FakeRun(task_result_id=kwargs.get("task_result_id"))
            created_runs.append(run)
            return run

        with (
            patch("sessions.queue.run_job_task") as mock_task,
            patch("sessions.services.acreate_run", new_callable=AsyncMock, side_effect=capture_acreate_run),
            patch("sessions.services.generate_batch_title_task"),
        ):
            mock_task.aenqueue = AsyncMock(return_value=await _make_task_row())
            mock_task.module_path = run_job_task.module_path
            response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="x"))

        body = response.json()
        assert len(created_runs) == 1
        assert body["jobs"][0]["job_id"] == str(created_runs[0].id)
        assert body["jobs"][0]["job_id"] != str(created_runs[0].task_result_id)

    async def test_empty_thread_id_rejected_at_schema(self, authenticated_client):
        """An empty-string thread_id is malformed at the protocol layer (422, not 400)."""
        body = _single_repo_body(prompt="x", thread_id="")
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 422

    async def test_malformed_thread_id_rejected_at_schema(self, authenticated_client):
        """A non-UUID thread_id is rejected by the schema, not silently mapped to 'not found'."""
        body = _single_repo_body(prompt="x", thread_id="not-a-uuid")
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
class TestFollowUpThroughTheInbox:
    async def _session(self, **fields) -> Session:
        user = await User.objects.aget(username="testuser")
        return await Session.objects.acreate(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="group/project", ref="main", user=user,
            **fields,
        )  # fmt: skip

    async def _busy(self) -> Session:
        return await self._session(active_run_id="chat-run", last_active_at=timezone.now())

    async def _submit(self, client, session: Session, **repo) -> dict:
        body = {"repos": [{"repo_id": "group/project", "ref": None, **repo}], "prompt": "Bump it"}
        response = await client.post("/jobs", json=body | {"thread_id": session.thread_id})
        assert response.status_code == 202
        return response.json()

    async def test_on_an_idle_session_it_returns_the_run_the_inbox_started(self, authenticated_client):
        session = await self._session()

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=await _make_task_row())
            body = await self._submit(authenticated_client, session)

        [job] = body["jobs"]
        run = await Run.objects.aget(session=session)
        assert (job["job_id"], job["status"], job["thread_id"]) == (str(run.pk), "READY", session.thread_id)
        assert (run.trigger_type, run.prompt) == (SessionOrigin.API_JOB, "Bump it")
        event = await InboxEvent.objects.aget(session=session)
        assert (event.kind, event.run_id) == (EventKind.FOLLOW_UP_JOB, run.pk)
        assert mock_task.aenqueue.call_args.kwargs["inbox"] is True

    async def test_jobs_on_one_branch_of_a_busy_session_share_its_next_run(self, authenticated_client):
        session = await self._busy()

        with patch("sessions.queue.run_job_task") as mock_task:
            first = await self._submit(authenticated_client, session)
            second = await self._submit(authenticated_client, session)

        assert first["jobs"][0]["job_id"] == second["jobs"][0]["job_id"]
        assert first["jobs"][0]["status"] == "QUEUED"
        assert await Run.objects.filter(session=session).acount() == 1
        mock_task.aenqueue.assert_not_called()

    async def test_jobs_on_different_branches_get_different_next_runs(self, authenticated_client):
        session = await self._busy()

        with patch("sessions.queue.run_job_task"):
            here = await self._submit(authenticated_client, session)
            there = await self._submit(authenticated_client, session, ref="feat/other")

        assert here["jobs"][0]["job_id"] != there["jobs"][0]["job_id"]
        event = await InboxEvent.objects.aget(session=session, ref="feat/other")
        assert event.payload["ref"] == "feat/other"
        assert await Run.objects.filter(pk=there["jobs"][0]["job_id"], ref="feat/other").aexists()

    async def test_its_references_join_the_session_once_a_run_takes_it(self, authenticated_client):
        session = await self._session()

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=await _make_task_row())
            response = await authenticated_client.post(
                "/jobs",
                json=_single_repo_body(
                    thread_id=session.thread_id,
                    references=[{"key": "PROJ-9", "provider": "jira", "url": "https://jira.example.com/browse/PROJ-9"}],
                ),
            )

        assert response.status_code == 202
        await session.arefresh_from_db()
        assert [ref["key"] for ref in session.external_refs] == ["PROJ-9"]


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_queued_passes_through(authenticated_client: TestAsyncClient):
    """A QUEUED Run surfaces as ``status='QUEUED'`` in get_job_status (not 'PENDING')."""
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status=RunStatus.QUEUED)
    response = await authenticated_client.get(f"/jobs/{run.id}")
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"


@pytest.mark.django_db(transaction=True)
async def test_thread_validation_uses_session_ownership(authenticated_client: TestAsyncClient):
    """Continuation of a thread whose session belongs to someone else -> 400 opaque error."""
    other = await User.objects.acreate_user(username="outsider", email="outsider@test.com", password="x")  # noqa: S106
    thread = str(uuid.uuid4())
    session = await Session.objects.acreate(thread_id=thread, origin=SessionOrigin.API_JOB, repo_id="a/b", user=other)
    await Run.objects.acreate(session=session, trigger_type=SessionOrigin.API_JOB, repo_id="a/b", user=other)
    body = _single_repo_body(prompt="x", thread_id=thread)
    response = await authenticated_client.post("/jobs", json=body)
    assert response.status_code == 400
    assert "thread_id not found" in response.json()["detail"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_denied_repo_returns_opaque_404(authenticated_client: TestAsyncClient):
    from codebase.authorization import RepositoryAccessDenied

    with patch("sessions.admission.assert_can_run", side_effect=RepositoryAccessDenied(["group/project"])):
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="p"))

    assert response.status_code == 404
    assert response.json()["detail"] == "Repository not found or not accessible."


@pytest.mark.django_db(transaction=True)
async def test_submit_job_access_revoked_before_submit_returns_opaque_404(authenticated_client: TestAsyncClient):
    from codebase.authorization import RepositoryAccessDenied

    revoked = [None, RepositoryAccessDenied(["group/project"])]
    with patch("sessions.admission.assert_can_run", side_effect=revoked):
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="p"))

    assert response.status_code == 404
    assert response.json()["detail"] == "Repository not found or not accessible."
    assert await Run.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_submit_job_with_references_persists_them(authenticated_client: TestAsyncClient):
    body = _single_repo_body(
        references=[{"key": "PROJ-9", "provider": "jira", "url": "https://jira.example.com/browse/PROJ-9"}]
    )
    with patch("sessions.queue.run_job_task") as mock_task:
        mock_task.aenqueue.side_effect = lambda **kwargs: _make_task_row()
        response = await authenticated_client.post("/jobs", json=body)

    assert response.status_code == 202
    thread_id = response.json()["jobs"][0]["thread_id"]
    session = await Session.objects.aget(thread_id=thread_id)
    assert session.external_refs == [
        {"key": "PROJ-9", "provider": "jira", "url": "https://jira.example.com/browse/PROJ-9", "relation": "relates"}
    ]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_with_malformed_reference_is_422(authenticated_client: TestAsyncClient):
    body = _single_repo_body(references=[{"key": "bad key"}])
    response = await authenticated_client.post("/jobs", json=body)
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_lists_artifacts(authenticated_client: TestAsyncClient):
    from django.contrib.sites.models import Site

    from asgiref.sync import sync_to_async

    await Site.objects.aupdate_or_create(pk=1, defaults={"domain": "daiv.example.com", "name": "DAIV"})
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status="SUCCESSFUL", result_summary="Report published")
    artifact = await sync_to_async(make_artifact)(run, filename="audit.html", content=b"<p>x</p>", title="Audit")

    response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["artifacts"] == [
        {
            "id": str(artifact.pk),
            "title": "Audit",
            "filename": "audit.html",
            "content_type": "text/html",
            "kind": "html",
            "size": 8,
            "url": f"https://daiv.example.com/dashboard/sessions/{run.session_id}/artifacts/{artifact.pk}/",
            "download_url": (
                f"https://daiv.example.com/dashboard/sessions/{run.session_id}/artifacts/{artifact.pk}/raw/?download=1"
            ),
        }
    ]


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_without_artifacts_returns_empty_list(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status="RUNNING")
    response = await authenticated_client.get(f"/jobs/{run.id}")
    assert response.json()["artifacts"] == []
    assert response.json()["artifacts_error"] is None


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_still_answers_when_artifacts_cannot_be_listed(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status="SUCCESSFUL", result_summary="done")

    with patch("sessions.artifacts.aserialize_run_artifacts", AsyncMock(side_effect=RuntimeError("no site"))):
        response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["result"] == "done"
    assert data["artifacts"] == []
    assert "could not be listed" in data["artifacts_error"]


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_failed_by_a_provider_says_why(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status="FAILED", error_message="Traceback: raw")
    await Run.objects.filter(pk=run.pk).aupdate(failure_kind="out_of_credits", failure_provider="openrouter")
    response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.json()["error"] == failure_message("out_of_credits", "openrouter")


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_failed_otherwise_keeps_the_generic_error(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status="FAILED", error_message="Traceback: raw")
    response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.json()["error"] == "Job execution failed"


# --- Merge request target ---


def _merge_request_body(**overrides):
    return {"repos": [{"repo_id": "group/project", "merge_request": 7}], "prompt": "/code-review"} | overrides


def _open_merge_request(**fields):
    from codebase.base import MergeRequest
    from codebase.base import User as PlatformUser

    return MergeRequest(
        **{
            "repo_id": "group/project",
            "merge_request_id": 7,
            "source_branch": "feat",
            "target_branch": "main",
            "title": "Feature",
            "description": "",
            "web_url": "https://git.test/group/project/-/merge_requests/7",
            "author": PlatformUser(id=1, username="author"),
        }
        | fields
    )


@pytest.fixture
def merge_request_platform(mock_repo_client):
    from django.core.cache import cache

    async_to_sync(cache.aclear)()
    mock_repo_client.get_merge_request.return_value = _open_merge_request()
    with patch("sessions.merge_request_runs.starter.resolve_env_for_run", new=AsyncMock(return_value=None)):
        yield mock_repo_client


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "entry",
    [
        {"repo_id": "group/project", "merge_request": 7, "ref": "main"},
        {"repo_id": "group/project", "merge_request": 7, "prompt": "only here"},
        {"repo_id": "group/project", "merge_request": 0},
    ],
    ids=["with-ref", "with-prompt", "not-positive"],
)
async def test_a_merge_request_target_with_a_ref_or_its_own_prompt_is_refused(authenticated_client, entry):
    response = await authenticated_client.post("/jobs", json={"repos": [entry], "prompt": "p"})

    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "extra", [{"shared_session": True}, {"thread_id": str(uuid.uuid4())}], ids=["shared", "thread"]
)
async def test_a_merge_request_target_in_a_shared_or_continued_session_is_refused(authenticated_client, extra):
    response = await authenticated_client.post("/jobs", json=_merge_request_body(**extra))

    assert response.status_code == 400
    assert response.json()["detail"] == "A merge request target can't be combined with shared_session or thread_id."


@pytest.mark.django_db(transaction=True)
async def test_a_merge_request_target_beside_another_repository_is_refused(authenticated_client):
    body = _merge_request_body(repos=[{"repo_id": "group/project", "merge_request": 7}, {"repo_id": "group/other"}])

    response = await authenticated_client.post("/jobs", json=body)

    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
async def test_a_merge_request_target_without_write_access_answers_404_unconfirmed(
    authenticated_client, merge_request_platform
):
    from codebase.authorization import RepositoryAccessDenied

    with patch("sessions.admission.assert_can_run", side_effect=RepositoryAccessDenied(["group/project"])):
        response = await authenticated_client.post("/jobs", json=_merge_request_body())

    assert response.status_code == 404
    merge_request_platform.get_merge_request.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("platform", "error"),
    [({"merged": True}, "Merge request !7 is merged."), ({"closed": True}, "Merge request !7 is closed.")],
)
async def test_a_merge_request_that_is_not_open_is_a_failed_entry(
    authenticated_client, merge_request_platform, platform, error
):
    merge_request_platform.get_merge_request.return_value = _open_merge_request(**platform)

    response = await authenticated_client.post("/jobs", json=_merge_request_body())

    assert response.status_code == 202
    assert response.json()["jobs"] == []
    assert response.json()["failed"] == [{"repo_id": "group/project", "ref": "", "error": error}]
    assert not await Run.objects.aexists()


@pytest.mark.django_db(transaction=True)
async def test_a_missing_merge_request_is_a_failed_entry(authenticated_client, merge_request_platform):
    from gitlab.exceptions import GitlabGetError

    merge_request_platform.get_merge_request.side_effect = GitlabGetError("404 Not Found", response_code=404)

    response = await authenticated_client.post("/jobs", json=_merge_request_body())

    assert response.json()["failed"][0]["error"] == "Merge request !7 was not found in group/project."


@pytest.mark.django_db(transaction=True)
async def test_a_merge_request_whose_session_is_archived_is_a_failed_entry(
    authenticated_client, merge_request_platform
):
    from sessions.ids import compute_thread_id

    from codebase.base import Scope

    thread_id = compute_thread_id(repo_slug="group/project", scope=Scope.MERGE_REQUEST, entity_iid=7)
    await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.MR_WEBHOOK, repo_id="group/project", archived_at=timezone.now()
    )

    response = await authenticated_client.post("/jobs", json=_merge_request_body())

    error = response.json()["failed"][0]["error"]
    assert error.startswith("The DAIV session of merge request !7 is archived. Unarchive it to run jobs on it: ")
    assert thread_id in error
    merge_request_platform.create_merge_request_comment.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("database_task_backend")
async def test_a_job_on_a_merge_request_no_session_owns_runs_in_its_session(
    authenticated_client, merge_request_platform
):
    from sessions.ids import compute_thread_id

    from codebase.base import Scope

    response = await authenticated_client.post("/jobs", json=_merge_request_body())

    assert response.status_code == 202
    [job] = response.json()["jobs"]
    run = await Run.objects.select_related("task_result").aget()
    assert (job["job_id"], job["status"]) == (str(run.pk), "READY")
    assert job["thread_id"] == compute_thread_id(repo_slug="group/project", scope=Scope.MERGE_REQUEST, entity_iid=7)
    assert (run.trigger_type, run.merge_request_iid, run.ref) == (SessionOrigin.API_JOB, 7, "feat")
    assert run.task_result.task_path == "sessions.merge_request_runs.tasks.address_merge_request_task"
    assert run.task_result.args_kwargs["kwargs"]["request_key"].startswith("job:")

    status = await authenticated_client.get(f"/jobs/{run.pk}")
    assert status.json()["merge_request_url"] == "https://git.test/group/project/-/merge_requests/7"


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("database_task_backend")
async def test_a_job_on_a_merge_request_runs_with_its_references_and_mcp_overrides(
    authenticated_client, merge_request_platform
):
    await sync_to_async(only_servers)(("grafana", MCPServer.Status.ON_DEMAND))
    body = _merge_request_body(mcp_overrides={"grafana": True}, references=[{"key": "PROJ-1"}])

    response = await authenticated_client.post("/jobs", json=body)

    assert response.status_code == 202
    run = await Run.objects.select_related("task_result").aget()
    kwargs = run.task_result.args_kwargs["kwargs"]
    assert kwargs["mcp_overrides"] == {"grafana": "on"}
    assert [ref["key"] for ref in kwargs["references"]] == ["PROJ-1"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("database_task_backend")
@pytest.mark.parametrize(
    ("extra_data", "expected"), [({"username": "jdoe"}, "jdoe"), (None, "")], ids=["linked", "unlinked"]
)
async def test_a_job_on_a_merge_request_names_its_caller_by_their_linked_account_only(
    authenticated_client, merge_request_platform, extra_data, expected
):
    from allauth.socialaccount.models import SocialAccount

    from codebase.base import GitPlatform
    from codebase.conf import settings as codebase_settings

    if extra_data is not None:
        user = await User.objects.aget(username="testuser")
        await SocialAccount.objects.acreate(user=user, provider="gitlab", uid="5", extra_data=extra_data)

    with patch.object(codebase_settings, "CLIENT", GitPlatform.GITLAB):
        response = await authenticated_client.post("/jobs", json=_merge_request_body())

    assert response.status_code == 202
    run = await Run.objects.select_related("task_result").aget()
    assert run.task_result.args_kwargs["kwargs"]["requester_username"] == expected
    assert run.external_username == expected


@pytest.mark.django_db(transaction=True)
async def test_jobs_on_a_merge_request_a_busy_session_owns_wait_as_turns_of_their_own(
    authenticated_client, merge_request_platform
):
    from tests.unit_tests.sessions.conftest import amake_owned_merge_request

    owned = await amake_owned_merge_request(repo_id="group/project", merge_request_iid=7)
    await Session.objects.filter(pk=owned.session_id).aupdate(active_run_id="other", last_active_at=timezone.now())

    first = await authenticated_client.post("/jobs", json=_merge_request_body())
    second = await authenticated_client.post("/jobs", json=_merge_request_body())

    [job_one], [job_two] = first.json()["jobs"], second.json()["jobs"]
    assert (job_one["status"], job_two["status"]) == ("QUEUED", "QUEUED")
    assert job_one["job_id"] != job_two["job_id"]
    assert job_one["thread_id"] == job_two["thread_id"] == owned.session_id
    events = InboxEvent.objects.filter(session_id=owned.session_id, kind=EventKind.MERGE_REQUEST_JOB)
    assert await events.acount() == 2


@pytest.mark.django_db(transaction=True)
async def test_a_job_that_did_not_start_says_why(authenticated_client):
    user = await User.objects.aget(username="testuser")
    note = "This merge request was closed before the job could start, so I didn't run it."
    run = await _create_run_row(user, status="FAILED", error_message=note)

    response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.json()["error"] == note


# --- CI identity tokens ---


@pytest.fixture
def ci_token(mock_repo_client):
    """Mint GitLab CI identity tokens for ``group/project`` (platform id 1), verified against in-memory signing keys."""
    from django.core.cache import cache

    import jwt

    from accounts.api.ci_identity import CIIdentityVerifier
    from codebase.base import GitPlatform
    from codebase.conf import settings as codebase_settings
    from core.conf import settings as core_settings
    from tests.unit_tests.accounts.api.test_ci_identity import SIGNING_KEY, gitlab_claims, token

    async_to_sync(cache.aclear)()
    CIIdentityVerifier._key_clients.clear()
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(SIGNING_KEY.public_key(), as_dict=True) | {"kid": "k1", "alg": "RS256"}
    mock_repo_client.get_repository.return_value = mock_repo_client.get_repository.return_value.model_copy(
        update={"pk": 1, "slug": "group/project"}
    )

    def mint(**claims):
        return token(gitlab_claims(project_id="1", **claims))

    with (
        patch.object(codebase_settings, "CLIENT", GitPlatform.GITLAB),
        patch.object(codebase_settings, "GITLAB_URL", "https://gitlab.example.com"),
        patch.object(core_settings, "EXTERNAL_URL", "https://daiv.example.com/"),
        patch.object(jwt.PyJWKClient, "fetch_data", return_value={"keys": [jwk]}),
        patch("jobs.api.ci.RepositoryConfig.get_config", return_value=_repo_config()),
    ):
        yield mint
    CIIdentityVerifier._key_clients.clear()


def _repo_config(**fields):
    from codebase.repo_config import RepositoryConfig

    return RepositoryConfig(**fields)


def _ci_client(raw_token):
    return TestAsyncClient(api, headers={"Authorization": f"Bearer {raw_token}"})


@contextmanager
def _enqueued_run_job():
    with patch("sessions.queue.run_job_task") as task:
        task.aenqueue.side_effect = lambda **kwargs: _make_task_row()
        task.module_path = run_job_task.module_path
        yield task


def _ci_body(**overrides):
    return {"repos": [{"repo_id": "group/project"}], "prompt": "/code-review"} | overrides


@pytest.mark.django_db(transaction=True)
class TestCIIdentitySubmissions:
    @pytest.mark.parametrize(
        "body",
        [
            _ci_body(repos=[{"repo_id": "group/project"}, {"repo_id": "group/other"}]),
            _ci_body(thread_id=str(uuid.uuid4())),
            _ci_body(shared_session=True),
            _ci_body(references=[{"key": "X-1", "provider": "jira", "url": "https://j/X-1"}]),
        ],
        ids=["two-repos", "thread", "shared", "references"],
    )
    async def test_anything_but_one_job_on_its_repository_is_refused(self, ci_token, body):
        response = await _ci_client(ci_token()).post("/jobs", json=body)

        assert response.status_code == 400
        assert response.json()["detail"] == "A CI identity token submits one job on its own repository."

    async def test_another_repository_is_unauthorized(self, ci_token, mock_repo_client, caplog):
        mock_repo_client.get_repository.return_value = mock_repo_client.get_repository.return_value.model_copy(
            update={"pk": 99}
        )

        response = await _ci_client(ci_token()).post("/jobs", json=_ci_body())

        assert response.status_code == 401
        assert "(repository)" in caplog.text

    async def test_the_allowlist_is_read_with_the_repository_already_verified(self, ci_token, mock_repo_client):
        with patch(
            "jobs.api.ci.RepositoryConfig.get_config", return_value=_repo_config(allowed_usernames=["alice"])
        ) as get_config:
            await _ci_client(ci_token()).post("/jobs", json=_ci_body())

        assert get_config.call_args.kwargs["repository"] is mock_repo_client.get_repository.return_value
        mock_repo_client.get_repository.assert_called_once()

    async def test_an_actor_outside_the_allowlist_is_unauthorized(self, ci_token, caplog):
        with patch("jobs.api.ci.RepositoryConfig.get_config", return_value=_repo_config(allowed_usernames=["alice"])):
            response = await _ci_client(ci_token()).post("/jobs", json=_ci_body())

        assert response.status_code == 401
        assert "(allowed usernames)" in caplog.text

    async def test_a_personal_mcp_server_is_refused(self, ci_token):
        from tests.unit_tests.mcp_connectors.helpers import make_server

        owner = await User.objects.acreate(username="owner", email="o@test.com")
        await sync_to_async(only_servers)(("rt", MCPServer.Status.ACTIVE))
        await sync_to_async(make_server)("mine", scope=MCPServer.Visibility.USER, user=owner)

        response = await _ci_client(ci_token()).post("/jobs", json=_ci_body(mcp_overrides={"mine": True}))

        assert response.status_code == 400

    async def test_an_unlinked_actor_gets_an_external_run_that_acts_as_no_one_and_is_muted(self, ci_token):
        with _enqueued_run_job() as task:
            response = await _ci_client(ci_token()).post("/jobs", json=_ci_body())

        assert response.status_code == 202
        run = await Run.objects.aget(pk=response.json()["jobs"][0]["job_id"])
        assert (run.user_id, run.external_provider, run.external_uid, run.external_username) == (
            None, "gitlab", "9", "pusher",
        )  # fmt: skip
        assert (run.acts_as_user, run.muted) == (False, True)
        assert task.aenqueue.call_args.kwargs["user_id"] is None

    @pytest.mark.parametrize("active", [True, False], ids=["active", "inactive"])
    async def test_a_linked_actor_owns_the_run_only_while_active(self, ci_token, active):
        from allauth.socialaccount.models import SocialAccount

        user = await User.objects.acreate(username="linked", email="l@test.com", is_active=active)
        await SocialAccount.objects.acreate(user=user, provider="gitlab", uid="9")

        with _enqueued_run_job() as task:
            response = await _ci_client(ci_token()).post("/jobs", json=_ci_body(muted=False))

        run = await Run.objects.aget(pk=response.json()["jobs"][0]["job_id"])
        assert run.user_id == (user.pk if active else None)
        assert (run.acts_as_user, run.muted, run.external_uid) == (False, False, "9")
        assert task.aenqueue.call_args.kwargs["user_id"] is None

    async def test_a_token_submits_one_accepted_job(self, ci_token, caplog):
        raw = ci_token()
        with _enqueued_run_job():
            first = await _ci_client(raw).post("/jobs", json=_ci_body())
            second = await _ci_client(raw).post("/jobs", json=_ci_body())

        assert (first.status_code, second.status_code) == (202, 401)
        assert "(reused)" in caplog.text
        polled = await _ci_client(raw).get(f"/jobs/{first.json()['jobs'][0]['job_id']}")
        assert polled.status_code == 200

    async def test_a_refused_submission_does_not_use_the_token_up(self, ci_token):
        raw = ci_token()

        refused = await _ci_client(raw).post("/jobs", json=_ci_body(shared_session=True))
        with _enqueued_run_job():
            accepted = await _ci_client(raw).post("/jobs", json=_ci_body())

        assert (refused.status_code, accepted.status_code) == (400, 202)

    async def test_a_submission_that_accepted_no_job_does_not_use_the_token_up(self, ci_token, mock_repo_client):
        raw = ci_token()
        mock_repo_client.get_merge_request.return_value = mock_repo_client.get_merge_request.return_value.model_copy(
            update={"merged": True}
        )
        body = {"repos": [{"repo_id": "group/project", "merge_request": 1}], "prompt": "/code-review"}

        failed = await _ci_client(raw).post("/jobs", json=body)
        with _enqueued_run_job():
            accepted = await _ci_client(raw).post("/jobs", json=_ci_body())

        assert (failed.status_code, failed.json()["jobs"], accepted.status_code) == (202, [], 202)

    async def test_a_merge_request_job_is_attributed_to_the_actor_and_acts_as_no_one(self, ci_token, mock_repo_client):
        with (
            patch("sessions.merge_request_runs.starter.resolve_env_for_run", new=AsyncMock(return_value=None)),
            patch("sessions.merge_request_runs.service.address_merge_request_task") as task,
        ):
            task.get_or_enqueue.return_value = (await _make_task_row(), True)
            response = await _ci_client(ci_token()).post(
                "/jobs", json={"repos": [{"repo_id": "group/project", "merge_request": 1}], "prompt": "/code-review"}
            )

        run = await Run.objects.aget(pk=response.json()["jobs"][0]["job_id"])
        assert (run.merge_request_iid, run.user_id, run.external_uid, run.acts_as_user, run.muted) == (
            1, None, "9", False, True,
        )  # fmt: skip


@pytest.mark.django_db(transaction=True)
class TestCIIdentityPolling:
    async def _run(self, **fields):
        session = await Session.objects.acreate(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id=fields.get("repo_id", "group/project")
        )
        return await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.API_JOB, status=RunStatus.SUCCESSFUL,
            **({"repo_id": "group/project"} | fields),
        )  # fmt: skip

    async def test_a_run_of_its_repository_attributed_to_its_actor_is_visible(self, ci_token):
        run = await self._run(external_provider="gitlab", external_uid="9")

        assert (await _ci_client(ci_token()).get(f"/jobs/{run.pk}")).status_code == 200

    async def test_any_other_run_is_not_found(self, ci_token):
        run = await self._run(external_provider="gitlab", external_uid="10")

        assert (await _ci_client(ci_token()).get(f"/jobs/{run.pk}")).status_code == 404
