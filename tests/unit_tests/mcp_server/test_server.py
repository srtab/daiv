import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_server.server import get_job_status, list_repositories, submit_job

from codebase.base import GitPlatform, Repository


def _mock_task():
    m = MagicMock()
    m.id = str(uuid.uuid4())
    return m


class _FakeActivity:
    _next_pk = 0

    def __init__(self, task_result_id):
        self.task_result_id = task_result_id
        type(self)._next_pk += 1
        self.pk = type(self)._next_pk


async def _fake_acreate_activity(**kwargs):
    return _FakeActivity(task_result_id=kwargs["task_result_id"])


def _patch_acreate():
    # Patch acreate_activity and silence the post-create title task enqueue so
    # tests don't depend on the queue backend.
    acreate_patch = patch(
        "activity.services.acreate_activity", new_callable=AsyncMock, side_effect=_fake_acreate_activity
    )
    title_patch = patch("activity.services.generate_title_task")

    class _Combined:
        def __enter__(self):
            mock_create = acreate_patch.__enter__()
            mock_title = title_patch.__enter__()
            mock_title.aenqueue = AsyncMock(return_value=None)
            return mock_create

        def __exit__(self, exc_type, exc, tb):
            title_patch.__exit__(exc_type, exc, tb)
            return acreate_patch.__exit__(exc_type, exc, tb)

    return _Combined()


@pytest.mark.django_db(transaction=True)
async def test_submit_job_single_repo_returns_batch_response():
    with patch("activity.services.run_job_task") as mock_task, _patch_acreate():
        mock_task.aenqueue = AsyncMock(return_value=_mock_task())
        result = await submit_job(repos=[{"repo_id": "group/project", "ref": None}], prompt="Fix the bug")

    data = json.loads(result)
    assert "batch_id" in data
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["repo_id"] == "group/project"
    assert data["jobs"][0]["ref"] is None
    assert data["failed"] == []


@pytest.mark.django_db(transaction=True)
async def test_submit_job_multi_repo_enqueues_each():
    tasks = [_mock_task() for _ in range(3)]
    call_log = []

    async def _aenqueue(**kwargs):
        call_log.append(kwargs)
        return tasks[len(call_log) - 1]

    with patch("activity.services.run_job_task") as mock_task, _patch_acreate():
        mock_task.aenqueue = _aenqueue
        result = await submit_job(
            repos=[{"repo_id": "o/a", "ref": None}, {"repo_id": "o/b", "ref": "dev"}, {"repo_id": "o/c", "ref": ""}],
            prompt="p",
        )

    data = json.loads(result)
    assert len(data["jobs"]) == 3
    assert {j["repo_id"] for j in data["jobs"]} == {"o/a", "o/b", "o/c"}
    refs = [c["ref"] for c in call_log]
    assert None in refs and "dev" in refs


@pytest.mark.django_db(transaction=True)
async def test_submit_job_reports_partial_failure():
    async def _flaky(**kwargs):
        if kwargs["repo_id"] == "o/b":
            raise RuntimeError("boom")
        return _mock_task()

    with patch("activity.services.run_job_task") as mock_task, _patch_acreate():
        mock_task.aenqueue = _flaky
        result = await submit_job(repos=[{"repo_id": "o/a", "ref": None}, {"repo_id": "o/b", "ref": None}], prompt="p")

    data = json.loads(result)
    assert len(data["jobs"]) == 1
    assert len(data["failed"]) == 1
    assert data["failed"][0]["repo_id"] == "o/b"


@pytest.mark.django_db(transaction=True)
async def test_submit_job_passes_ref():
    with patch("activity.services.run_job_task") as mock_task, _patch_acreate():
        mock_task.aenqueue = AsyncMock(return_value=_mock_task())
        await submit_job(repos=[{"repo_id": "group/project", "ref": "feature-branch"}], prompt="Fix the bug")
        mock_task.aenqueue.assert_called_once()
        kwargs = mock_task.aenqueue.call_args.kwargs
        assert kwargs["repo_id"] == "group/project"
        assert kwargs["prompt"] == "Fix the bug"
        assert kwargs["ref"] == "feature-branch"
        assert kwargs["use_max"] is False
        assert kwargs["thread_id"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_forwards_use_max_to_activity():
    """MCP submit tool threads ``use_max`` into ``acreate_activity``."""
    with patch("activity.services.run_job_task") as mock_task, _patch_acreate() as mock_create:
        mock_task.aenqueue = AsyncMock(return_value=_mock_task())
        await submit_job(repos=[{"repo_id": "group/project", "ref": None}], prompt="Fix the bug", use_max=True)

    assert mock_create.await_args.kwargs["use_max"] is True


@pytest.mark.django_db(transaction=True)
async def test_submit_job_forwards_notify_on_to_activity():
    """MCP submit tool threads ``notify_on`` into ``acreate_activity``."""
    from notifications.choices import NotifyOn

    with patch("activity.services.run_job_task") as mock_task, _patch_acreate() as mock_create:
        mock_task.aenqueue = AsyncMock(return_value=_mock_task())
        await submit_job(repos=[{"repo_id": "group/project", "ref": None}], prompt="p", notify_on=NotifyOn.ALWAYS)

    assert mock_create.await_args.kwargs["notify_on"] == NotifyOn.ALWAYS


@pytest.mark.django_db(transaction=True)
async def test_submit_job_notify_on_defaults_to_none():
    """Omitting ``notify_on`` forwards ``None`` to the activity."""
    with patch("activity.services.run_job_task") as mock_task, _patch_acreate() as mock_create:
        mock_task.aenqueue = AsyncMock(return_value=_mock_task())
        await submit_job(repos=[{"repo_id": "group/project", "ref": None}], prompt="p")

    assert mock_create.await_args.kwargs["notify_on"] is None


@pytest.mark.django_db(transaction=True)
async def test_submit_job_all_fail():
    """When every enqueue fails, no jobs in response, all entries in failed."""
    with patch("activity.services.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(side_effect=Exception("DB down"))
        result = await submit_job(repos=[{"repo_id": "group/project", "ref": None}], prompt="Fix the bug")

    data = json.loads(result)
    assert data["jobs"] == []
    assert len(data["failed"]) == 1
    assert "group/project" in data["failed"][0]["repo_id"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_empty_batch_returns_error_json():
    result = await submit_job(repos=[], prompt="p")
    data = json.loads(result)
    assert "error" in data


@pytest.mark.django_db(transaction=True)
async def test_submit_job_oversized_batch_returns_error_json():
    repos = [{"repo_id": f"o/r{i}", "ref": None} for i in range(21)]
    result = await submit_job(repos=repos, prompt="p")
    data = json.loads(result)
    assert "error" in data


@pytest.mark.django_db(transaction=True)
async def test_submit_job_wait_success():
    job_id = str(uuid.uuid4())
    mock_result = MagicMock()
    mock_result.id = job_id

    now = datetime.now(UTC)

    class _AsyncRows:
        def __init__(self, rows):
            self._rows = rows

        def __aiter__(self):
            return self._aiter()

        async def _aiter(self):
            for r in self._rows:
                yield r

    finished = MagicMock()
    finished.id = uuid.UUID(job_id)
    finished.status = "SUCCESSFUL"
    finished.return_value = "All done"
    finished.enqueued_at = now
    finished.started_at = now
    finished.finished_at = now

    with (
        patch("activity.services.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        _patch_acreate(),
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.objects.filter = MagicMock(return_value=_AsyncRows([finished]))

        result = await submit_job(repos=[{"repo_id": "group/project", "ref": None}], prompt="Fix the bug", wait=True)

    data = json.loads(result)
    assert "batch_id" in data
    assert len(data["statuses"]) == 1
    assert data["statuses"][0]["status"] == "SUCCESSFUL"
    assert data["statuses"][0]["result"] == "All done"


@pytest.mark.django_db(transaction=True)
async def test_submit_job_wait_pending_when_never_found():
    """When the batch poll times out without terminal results, statuses report PENDING."""
    mock_result = MagicMock()
    mock_result.id = str(uuid.uuid4())

    class _EmptyAsyncRows:
        def __aiter__(self):
            return self._aiter()

        async def _aiter(self):
            if False:
                yield None  # never yields

    with (
        patch("activity.services.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        _patch_acreate(),
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
        patch("mcp_server.server.MAX_POLL_DURATION", 4.0),
        patch("mcp_server.server.POLL_INTERVAL", 2.0),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.objects.filter = MagicMock(return_value=_EmptyAsyncRows())

        result = await submit_job(repos=[{"repo_id": "group/project", "ref": None}], prompt="Fix the bug", wait=True)

    data = json.loads(result)
    assert data["statuses"][0]["status"] == "PENDING"


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_not_found():
    result = await get_job_status(job_id=str(uuid.uuid4()))
    data = json.loads(result)
    assert data["error"] == "Job not found."


async def test_get_job_status_invalid_uuid():
    result = await get_job_status(job_id="not-a-uuid")
    data = json.loads(result)
    assert data["error"] == "Invalid job_id format."


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_wait_already_complete():
    """When wait=True but the job is already complete, return immediately."""
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    mock_db_result = MagicMock()
    mock_db_result.id = job_id
    mock_db_result.status = "SUCCESSFUL"
    mock_db_result.return_value = "Done"
    mock_db_result.enqueued_at = now
    mock_db_result.started_at = now
    mock_db_result.finished_at = now

    with patch("activity.services.run_job_task") as mock_task, patch("mcp_server.server.DBTaskResult") as mock_model:
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.objects.aget = AsyncMock(return_value=mock_db_result)
        mock_model.DoesNotExist = Exception

        result = await get_job_status(job_id=job_id, wait=True)

    data = json.loads(result)
    assert data["status"] == "SUCCESSFUL"
    assert data["result"] == "Done"
    # Should not have polled — only the initial fetch
    assert mock_model.objects.aget.call_count == 1


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_wait_polls_until_complete():
    """When wait=True and the job is still running, poll until complete."""
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    running_result = MagicMock()
    running_result.id = job_id
    running_result.status = "RUNNING"

    finished_result = MagicMock()
    finished_result.id = job_id
    finished_result.status = "SUCCESSFUL"
    finished_result.return_value = "Done"
    finished_result.enqueued_at = now
    finished_result.started_at = now
    finished_result.finished_at = now

    with (
        patch("activity.services.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_task.module_path = "jobs.tasks.run_job_task"
        # First call is the initial fetch (running), then polling finds it finished
        mock_model.objects.aget = AsyncMock(side_effect=[running_result, finished_result])
        mock_model.DoesNotExist = Exception

        result = await get_job_status(job_id=job_id, wait=True)

    data = json.loads(result)
    assert data["status"] == "SUCCESSFUL"
    # 1 initial fetch + 1 poll
    assert mock_model.objects.aget.call_count == 2


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_wait_not_found_then_appears():
    """When wait=True and the job doesn't exist yet, poll until it appears."""
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    finished_result = MagicMock()
    finished_result.id = job_id
    finished_result.status = "SUCCESSFUL"
    finished_result.return_value = "Done"
    finished_result.enqueued_at = now
    finished_result.started_at = now
    finished_result.finished_at = now

    class _DoesNotExistError(Exception):
        pass

    with (
        patch("activity.services.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.DoesNotExist = _DoesNotExistError
        # Initial fetch raises DoesNotExist, then poll finds it
        mock_model.objects.aget = AsyncMock(side_effect=[_DoesNotExistError, _DoesNotExistError, finished_result])

        result = await get_job_status(job_id=job_id, wait=True)

    data = json.loads(result)
    assert data["status"] == "SUCCESSFUL"


@pytest.mark.django_db(transaction=True)
async def test_submit_job_batch_poll_db_exception_breaks_loop():
    """DB error during batch polling terminates the loop; PENDING returned."""
    mock_result = MagicMock()
    mock_result.id = str(uuid.uuid4())

    with (
        patch("activity.services.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        _patch_acreate(),
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
        patch("mcp_server.server.MAX_POLL_DURATION", 4.0),
        patch("mcp_server.server.POLL_INTERVAL", 2.0),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.objects.filter = MagicMock(side_effect=RuntimeError("DB down"))

        result = await submit_job(repos=[{"repo_id": "group/project", "ref": None}], prompt="Fix the bug", wait=True)

    data = json.loads(result)
    # Loop breaks on DB error; statuses report PENDING for unresolved jobs.
    assert data["statuses"][0]["status"] == "PENDING"


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_db_exception():
    """Generic DB exception in get_job_status returns error response."""
    job_id = str(uuid.uuid4())

    class _DoesNotExistError(Exception):
        pass

    with patch("activity.services.run_job_task") as mock_task, patch("mcp_server.server.DBTaskResult") as mock_model:
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.DoesNotExist = _DoesNotExistError
        mock_model.objects.aget = AsyncMock(side_effect=RuntimeError("DB connection lost"))

        result = await get_job_status(job_id=job_id)

    data = json.loads(result)
    assert "error" in data
    assert "Failed to retrieve job status" in data["error"]


# ---------------------------------------------------------------------------
# Helpers for list_repositories / list_topics tests
# ---------------------------------------------------------------------------


def _make_repo(slug: str, name: str, topics: list[str] | None = None) -> Repository:
    return Repository(
        pk=1,
        slug=slug,
        name=name,
        clone_url=f"https://example.com/{slug}.git",
        html_url=f"https://example.com/{slug}",
        default_branch="main",
        git_platform=GitPlatform.GITLAB,
        topics=topics or [],
    )


SAMPLE_REPOS = [
    _make_repo("group/alpha", "alpha", ["python", "backend"]),
    _make_repo("group/beta", "beta", ["python", "frontend"]),
    _make_repo("group/gamma", "gamma", ["rust"]),
]


# ---------------------------------------------------------------------------
# list_repositories tests
# ---------------------------------------------------------------------------


async def test_list_repositories_default():
    mock_client = MagicMock()
    mock_client.list_repositories.return_value = SAMPLE_REPOS

    with patch("mcp_server.server.RepoClient") as mock_rc:
        mock_rc.create_instance.return_value = mock_client
        result = await list_repositories()

    data = json.loads(result)
    assert len(data["repositories"]) == 3
    assert data["repositories"][0]["slug"] == "group/alpha"
    assert data["repositories"][0]["topics"] == ["python", "backend"]
    assert "warning" not in data
    mock_client.list_repositories.assert_called_once_with(search=None, topics=None, limit=41)


async def test_list_repositories_with_search():
    mock_client = MagicMock()
    mock_client.list_repositories.return_value = [SAMPLE_REPOS[0]]

    with patch("mcp_server.server.RepoClient") as mock_rc:
        mock_rc.create_instance.return_value = mock_client
        result = await list_repositories(search="alpha")

    data = json.loads(result)
    assert len(data["repositories"]) == 1
    assert data["repositories"][0]["slug"] == "group/alpha"
    mock_client.list_repositories.assert_called_once_with(search="alpha", topics=None, limit=41)


async def test_list_repositories_with_topics():
    mock_client = MagicMock()
    mock_client.list_repositories.return_value = [SAMPLE_REPOS[0], SAMPLE_REPOS[1]]

    with patch("mcp_server.server.RepoClient") as mock_rc:
        mock_rc.create_instance.return_value = mock_client
        result = await list_repositories(topics=["python"])

    data = json.loads(result)
    assert len(data["repositories"]) == 2
    mock_client.list_repositories.assert_called_once_with(search=None, topics=["python"], limit=41)


async def test_list_repositories_truncated_with_warning():
    """When client returns more than MAX_REPOSITORIES, result is truncated with a warning."""
    # The tool fetches MAX_REPOSITORIES + 1 to detect truncation
    many_repos = [_make_repo(f"group/repo-{i}", f"repo-{i}") for i in range(41)]
    mock_client = MagicMock()
    mock_client.list_repositories.return_value = many_repos

    with patch("mcp_server.server.RepoClient") as mock_rc:
        mock_rc.create_instance.return_value = mock_client
        result = await list_repositories()

    data = json.loads(result)
    assert len(data["repositories"]) == 40
    assert "warning" in data
    assert "first 40" in data["warning"]
    # Verify limit was passed to the client
    mock_client.list_repositories.assert_called_once_with(search=None, topics=None, limit=41)


async def test_list_repositories_error_handling():
    mock_client = MagicMock()
    mock_client.list_repositories.side_effect = RuntimeError("API down")

    with patch("mcp_server.server.RepoClient") as mock_rc:
        mock_rc.create_instance.return_value = mock_client
        result = await list_repositories()

    data = json.loads(result)
    assert "error" in data
    assert "Failed to list repositories" in data["error"]
