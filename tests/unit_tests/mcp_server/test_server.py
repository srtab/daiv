import json
import uuid
from datetime import UTC, datetime
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from mcp_server.server import MAX_REPOSITORIES, get_job_status, list_repositories, submit_job


def _mock_task():
    m = MagicMock()
    m.id = str(uuid.uuid4())
    return m


class _FakeActivity:
    _next_pk = 0

    def __init__(self, task_result_id):
        self.id = uuid.uuid4()
        self.task_result_id = task_result_id
        self.thread_id = str(uuid.uuid4())
        self.status = "READY"
        type(self)._next_pk += 1
        self.pk = type(self)._next_pk
        # Tests bypass the real ORM via ``_patch_acreate``; provide an async no-op
        # so the post-acreate ``activity.asave(update_fields=...)`` call in
        # ``asubmit_batch_runs`` doesn't AttributeError on this stub.
        self.asave = AsyncMock(return_value=None)


async def _fake_acreate_activity(**kwargs):
    return _FakeActivity(task_result_id=kwargs["task_result_id"])


def _patch_acreate():
    # Patch acreate_activity and silence the post-create title task enqueue so
    # tests don't depend on the queue backend.
    acreate_patch = patch(
        "activity.services.acreate_activity", new_callable=AsyncMock, side_effect=_fake_acreate_activity
    )
    title_patch = patch("activity.services.generate_batch_title_task")

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


@pytest.fixture(autouse=True)
def _default_mcp_user(db):
    """Make ``get_current_user`` return a real authenticated user by default.

    ``submit_job`` and ``get_job_status`` reject ``mcp_user=None`` (defense in depth
    around the OAuth-bearer path) and downstream ORM filters require a real PK.
    Tests that explicitly test cross-user or unauthenticated paths patch the same
    name within their own ``with`` block, overriding this default.
    """
    from accounts.models import User

    user = User.objects.create_user(username="mcp_default", email="mcp@test.com", password="x")  # noqa: S106
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        yield user


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
        assert kwargs["agent_model"] is None
        assert kwargs["agent_thinking_level"] is None
        assert "use_max" not in kwargs
        assert kwargs["thread_id"]


@pytest.fixture
def openrouter_provider(db):
    from core.models import Provider, ProviderType

    Provider.objects.filter(slug="openrouter").delete()
    return Provider.objects.create(
        slug="openrouter", provider_type=ProviderType.OPENROUTER, api_key="sk-test", is_enabled=True
    )


@pytest.mark.django_db(transaction=True)
async def test_submit_job_rejects_unknown_provider():
    with _patch_acreate():
        payload = await submit_job(
            repos=[{"repo_id": "group/project", "ref": None}], prompt="Fix it", agent_model="bogus:nope"
        )
    body = json.loads(payload)
    assert "Unknown provider prefix" in body["error"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_rejects_when_no_model_and_no_system_default(monkeypatch):
    """The Auto fallback is gone: when the caller omits ``agent_model`` AND the admin
    hasn't configured a system default, the MCP tool refuses at submit time instead
    of letting the run reach the agent kickoff and explode with ``AgentConfigurationError``."""
    from core.site_settings import site_settings

    monkeypatch.setattr(site_settings, "agent_model_name", "")
    with _patch_acreate():
        payload = await submit_job(repos=[{"repo_id": "group/project", "ref": None}], prompt="Fix it")
    body = json.loads(payload)
    assert "system default" in body["error"].lower()


@pytest.mark.django_db(transaction=True)
async def test_submit_job_rejects_invalid_thinking_level(openrouter_provider):
    """The MCP tool's direct (in-process) call path bypasses FastMCP's protocol-layer
    Pydantic validation, so the explicit ``validate_agent_override`` call inside the
    tool must catch out-of-enum thinking levels."""
    with _patch_acreate():
        payload = await submit_job(
            repos=[{"repo_id": "group/project", "ref": None}],
            prompt="Fix it",
            agent_model="openrouter:anthropic/claude-haiku-4.5",
            agent_thinking_level="extreme",  # ty: ignore[invalid-argument-type]
        )
    body = json.loads(payload)
    assert "thinking level" in body["error"].lower()


@pytest.mark.django_db(transaction=True)
async def test_submit_job_forwards_agent_override(openrouter_provider):
    with patch("activity.services.run_job_task") as mock_task, _patch_acreate() as mock_create:
        mock_task.aenqueue = AsyncMock(return_value=_mock_task())
        await submit_job(
            repos=[{"repo_id": "group/project", "ref": None}],
            prompt="Fix the bug",
            agent_model="openrouter:anthropic/claude-haiku-4.5",
            agent_thinking_level="low",
        )

    assert mock_create.await_args.kwargs["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert mock_create.await_args.kwargs["agent_thinking_level"] == "low"
    enqueue_kwargs = mock_task.aenqueue.call_args.kwargs
    assert enqueue_kwargs["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert enqueue_kwargs["agent_thinking_level"] == "low"


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
async def test_submit_job_empty_repos_returns_error_json():
    result = await submit_job(repos=[], prompt="p")
    data = json.loads(result)
    assert "error" in data
    assert "At least one repository" in data["error"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_oversized_batch_returns_error_json():
    repos = [{"repo_id": f"o/r{i}", "ref": None} for i in range(21)]
    result = await submit_job(repos=repos, prompt="p")
    data = json.loads(result)
    assert "error" in data


@pytest.mark.django_db(transaction=True)
async def test_submit_job_wait_success():
    mock_result = MagicMock()
    mock_result.id = str(uuid.uuid4())

    now = datetime.now(UTC)
    created_activities: list[_FakeActivity] = []

    async def _capture_acreate(**kwargs):
        act = _FakeActivity(task_result_id=kwargs["task_result_id"])
        created_activities.append(act)
        return act

    class _AsyncRows:
        def __init__(self, rows):
            self._rows = rows

        def __aiter__(self):
            return self._aiter()

        async def _aiter(self):
            for r in self._rows:
                yield r

    from activity.models import ActivityStatus

    with (
        patch("activity.services.run_job_task") as mock_task,
        patch("activity.services.acreate_activity", new_callable=AsyncMock, side_effect=_capture_acreate),
        patch("activity.services.generate_batch_title_task") as mock_title,
        patch("mcp_server.server.Activity") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_title.aenqueue = AsyncMock(return_value=None)

        # We need to capture the activity ID after submit to build the finished mock.
        # Use a filter side effect that builds the row from the captured activity.
        def _make_filter(**kwargs):
            if not created_activities:
                return _AsyncRows([])
            finished = MagicMock()
            finished.id = created_activities[0].id
            finished.status = ActivityStatus.SUCCESSFUL
            finished.result_summary = "All done"
            finished.merge_request_web_url = ""
            finished.thread_id = None
            finished.created_at = now
            finished.started_at = now
            finished.finished_at = now
            return _AsyncRows([finished])

        mock_model.objects.filter = MagicMock(side_effect=lambda **kw: _make_filter(**kw))

        result = await submit_job(repos=[{"repo_id": "group/project", "ref": None}], prompt="Fix the bug", wait=True)

    data = json.loads(result)
    assert "batch_id" in data
    assert len(data["statuses"]) == 1
    assert data["statuses"][0]["status"] == "SUCCESSFUL"
    assert data["statuses"][0]["result"] == "All done"


@pytest.mark.django_db(transaction=True)
async def test_submit_job_wait_running_when_never_terminal():
    """When the batch poll times out without terminal results, statuses surface as RUNNING.

    RUNNING is the closest valid Activity status; PENDING is not in the documented enum.
    """
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
        patch("mcp_server.server.Activity") as mock_model,
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
    assert data["statuses"][0]["status"] == "RUNNING"


@pytest.mark.django_db(transaction=True)
async def test_submit_job_batch_poll_filters_by_authenticated_user(_default_mcp_user):
    """The batch poll must scope its Activity lookup by ``user=mcp_user`` to prevent
    cross-user reads. Asserts the call construction (not just behavior) so a refactor
    that drops the kwarg fails immediately."""
    from mcp_server.server import _poll_batch_until_complete

    captured: list[dict] = []

    class _EmptyAsyncRows:
        def __aiter__(self):
            return self._aiter()

        async def _aiter(self):
            if False:
                yield None

    def _capture_filter(**kwargs):
        captured.append(kwargs)
        return _EmptyAsyncRows()

    job_id = str(uuid.uuid4())
    with (
        patch("mcp_server.server.Activity") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
        patch("mcp_server.server.MAX_POLL_DURATION", 2.0),
        patch("mcp_server.server.POLL_INTERVAL", 2.0),
    ):
        mock_model.objects.filter = MagicMock(side_effect=_capture_filter)
        await _poll_batch_until_complete(
            "batch-1", [job_id], {"jobs": [{"job_id": job_id}], "failed": []}, _default_mcp_user
        )

    assert captured, "poll loop never called filter()"
    assert captured[0].get("user") is _default_mcp_user, (
        f"poll filter must be scoped by user=mcp_user; got kwargs={captured[0]!r}"
    )


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_not_found():
    caller = MagicMock(pk=1)

    class _DoesNotExistError(Exception):
        pass

    with (
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=caller)),
        patch("mcp_server.server.Activity") as mock_model,
    ):
        mock_model.DoesNotExist = _DoesNotExistError
        mock_model.objects.aget = AsyncMock(side_effect=_DoesNotExistError)
        result = await get_job_status(job_id=str(uuid.uuid4()))
    data = json.loads(result)
    assert data["error"] == "Job not found."


async def test_get_job_status_invalid_uuid():
    caller = MagicMock(pk=1)
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=caller)):
        result = await get_job_status(job_id="not-a-uuid")
    data = json.loads(result)
    assert data["error"] == "Invalid job_id format."


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_wait_already_complete():
    """When wait=True but the job is already complete, return immediately."""
    from activity.models import ActivityStatus

    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    mock_activity = MagicMock()
    mock_activity.id = uuid.UUID(job_id)
    mock_activity.status = ActivityStatus.SUCCESSFUL
    mock_activity.result_summary = "Done"
    mock_activity.merge_request_web_url = ""
    mock_activity.thread_id = None
    mock_activity.created_at = now
    mock_activity.started_at = now
    mock_activity.finished_at = now

    caller = MagicMock(pk=1)
    with (
        patch("mcp_server.server.Activity") as mock_model,
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=caller)),
    ):
        mock_model.objects.aget = AsyncMock(return_value=mock_activity)
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
    from activity.models import ActivityStatus

    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    running_result = MagicMock()
    running_result.id = uuid.UUID(job_id)
    running_result.status = ActivityStatus.RUNNING

    finished_result = MagicMock()
    finished_result.id = uuid.UUID(job_id)
    finished_result.status = ActivityStatus.SUCCESSFUL
    finished_result.result_summary = "Done"
    finished_result.merge_request_web_url = ""
    finished_result.thread_id = None
    finished_result.created_at = now
    finished_result.started_at = now
    finished_result.finished_at = now

    caller = MagicMock(pk=1)
    with (
        patch("mcp_server.server.Activity") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=caller)),
    ):
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
    from activity.models import ActivityStatus

    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    finished_result = MagicMock()
    finished_result.id = uuid.UUID(job_id)
    finished_result.status = ActivityStatus.SUCCESSFUL
    finished_result.result_summary = "Done"
    finished_result.merge_request_web_url = ""
    finished_result.thread_id = None
    finished_result.created_at = now
    finished_result.started_at = now
    finished_result.finished_at = now

    class _DoesNotExistError(Exception):
        pass

    caller = MagicMock(pk=1)
    with (
        patch("mcp_server.server.Activity") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=caller)),
    ):
        mock_model.DoesNotExist = _DoesNotExistError
        # Initial fetch raises DoesNotExist, then poll finds it
        mock_model.objects.aget = AsyncMock(side_effect=[_DoesNotExistError, _DoesNotExistError, finished_result])

        result = await get_job_status(job_id=job_id, wait=True)

    data = json.loads(result)
    assert data["status"] == "SUCCESSFUL"


@pytest.mark.django_db(transaction=True)
async def test_submit_job_batch_poll_db_exception_breaks_loop():
    """DB error during batch polling terminates the loop; unresolved jobs surface as RUNNING."""
    mock_result = MagicMock()
    mock_result.id = str(uuid.uuid4())

    with (
        patch("activity.services.run_job_task") as mock_task,
        patch("mcp_server.server.Activity") as mock_model,
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
    assert data["statuses"][0]["status"] == "RUNNING"


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_db_exception():
    """Generic DB exception in get_job_status returns error response."""
    job_id = str(uuid.uuid4())

    class _DoesNotExistError(Exception):
        pass

    caller = MagicMock(pk=1)
    with (
        patch("mcp_server.server.Activity") as mock_model,
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=caller)),
    ):
        mock_model.DoesNotExist = _DoesNotExistError
        mock_model.objects.aget = AsyncMock(side_effect=RuntimeError("DB connection lost"))

        result = await get_job_status(job_id=job_id)

    data = json.loads(result)
    assert "error" in data
    assert "Failed to retrieve job status" in data["error"]


# ---------------------------------------------------------------------------
# Helpers for list_repositories / list_topics tests
# ---------------------------------------------------------------------------


def _cat(slug: str, name: str, topics: list[str] | None = None):
    from codebase.models import RepositoryCatalog

    return RepositoryCatalog(
        provider="gitlab",
        slug=slug,
        name=name,
        default_branch="main",
        html_url=f"https://example.com/{slug}",
        topics=topics or [],
    )


# ---------------------------------------------------------------------------
# list_repositories tests
# ---------------------------------------------------------------------------


async def test_list_repositories_default():
    rows = [_cat("group/alpha", "alpha", ["python", "backend"]), _cat("group/beta", "beta")]

    with patch("mcp_server.server.asearch_viewable_repositories", new=AsyncMock(return_value=rows)) as mock_search:
        data = await list_repositories()

    assert [r["slug"] for r in data["repositories"]] == ["group/alpha", "group/beta"]
    assert data["repositories"][0]["topics"] == ["python", "backend"]
    assert data["repositories"][0]["default_branch"] == "main"
    assert data["repositories"][0]["html_url"] == "https://example.com/group/alpha"
    assert "warning" not in data
    assert data["next_cursor"] is None
    mock_search.assert_awaited_once_with(ANY, search=None, topics=None, limit=MAX_REPOSITORIES + 1)


async def test_list_repositories_with_search():
    with patch(
        "mcp_server.server.asearch_viewable_repositories", new=AsyncMock(return_value=[_cat("group/alpha", "alpha")])
    ) as mock_search:
        data = await list_repositories(search="alpha")

    assert [r["slug"] for r in data["repositories"]] == ["group/alpha"]
    mock_search.assert_awaited_once_with(ANY, search="alpha", topics=None, limit=MAX_REPOSITORIES + 1)


async def test_list_repositories_with_topics():
    rows = [_cat("group/alpha", "alpha", ["python"]), _cat("group/beta", "beta", ["python"])]

    with patch("mcp_server.server.asearch_viewable_repositories", new=AsyncMock(return_value=rows)) as mock_search:
        data = await list_repositories(topics=["python"])

    assert len(data["repositories"]) == 2
    mock_search.assert_awaited_once_with(ANY, search=None, topics=["python"], limit=MAX_REPOSITORIES + 1)


async def test_list_repositories_truncated_with_warning():
    """More than MAX_REPOSITORIES accessible → truncated to MAX with an exact overflow warning."""
    rows = [_cat(f"group/repo-{i}", f"repo-{i}") for i in range(MAX_REPOSITORIES + 1)]

    with patch("mcp_server.server.asearch_viewable_repositories", new=AsyncMock(return_value=rows)):
        data = await list_repositories()

    assert len(data["repositories"]) == MAX_REPOSITORIES
    assert "warning" in data
    assert "Not all accessible repositories" in data["warning"]
    assert data["next_cursor"] is None


async def test_list_repositories_error_handling():
    with patch("mcp_server.server.asearch_viewable_repositories", new=AsyncMock(side_effect=RuntimeError("DB down"))):
        data = await list_repositories()

    assert "error" in data
    assert "Failed to list repositories" in data["error"]


@pytest.mark.django_db(transaction=True)
class TestMCPThreadContinuation:
    async def test_response_includes_thread_id_and_status(self):
        with (
            patch("activity.services.run_job_task") as mock_task,
            _patch_acreate(),
            patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=MagicMock(pk=1))),
            patch("mcp_server.server.aresolve_repo_envs", new=AsyncMock(side_effect=lambda **kw: kw["repos"])),
        ):
            mock_task.aenqueue = AsyncMock(return_value=_mock_task())
            result = await submit_job(repos=[{"repo_id": "a/b", "ref": None}], prompt="x")
        data = json.loads(result)
        assert data["jobs"][0]["thread_id"]
        assert data["jobs"][0]["status"] in {"READY", "QUEUED"}

    async def test_unknown_thread_id_rejected(self):
        user = MagicMock(pk=1)
        with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
            result = await submit_job(repos=[{"repo_id": "a/b", "ref": None}], prompt="x", thread_id=str(uuid.uuid4()))
        data = json.loads(result)
        assert "thread_id not found" in data["error"]

    async def test_multi_repo_with_thread_id_rejected(self):
        user = MagicMock(pk=1)
        with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
            result = await submit_job(
                repos=[{"repo_id": "a/b", "ref": None}, {"repo_id": "c/d", "ref": None}],
                prompt="x",
                thread_id=str(uuid.uuid4()),
            )
        data = json.loads(result)
        assert "exactly one repo" in data["error"]

    async def test_malformed_thread_id_rejected(self):
        user = MagicMock(pk=1)
        with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
            result = await submit_job(repos=[{"repo_id": "a/b", "ref": None}], prompt="x", thread_id="not-a-uuid")
        data = json.loads(result)
        assert "thread_id not found" in data["error"]

    async def test_non_string_thread_id_rejected(self):
        """Pins the TypeError arm: direct callers passing a non-str/non-UUID value get the
        opaque error instead of an unhandled 500."""
        user = MagicMock(pk=1)
        with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
            result = await submit_job(repos=[{"repo_id": "a/b", "ref": None}], prompt="x", thread_id=12345)
        data = json.loads(result)
        assert "thread_id not found" in data["error"]

    async def test_unauthenticated_user_rejected(self):
        """Without a resolvable user, submit_job must reject — not silently submit as user=None."""
        with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=None)):
            result = await submit_job(repos=[{"repo_id": "a/b", "ref": None}], prompt="x")
        data = json.loads(result)
        assert "Authentication failed" in data["error"]


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_other_user_activity_returns_not_found():
    """An MCP caller cannot read another user's Activity by id."""
    from activity.models import Activity, ActivityStatus, TriggerType

    from accounts.models import User

    # One user owns the Activity
    owner = await User.objects.acreate_user(
        username="owner_mcp",
        email="owner_mcp@example.com",
        password="x",  # noqa: S106
    )
    activity = await Activity.objects.acreate(
        trigger_type=TriggerType.MCP_JOB, repo_id="a/b", status=ActivityStatus.SUCCESSFUL, user=owner
    )

    # A different caller tries to read the same Activity id
    caller = await User.objects.acreate_user(
        username="caller_mcp",
        email="caller_mcp@example.com",
        password="x",  # noqa: S106
    )
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=caller)):
        result = await get_job_status(job_id=str(activity.id))
    data = json.loads(result)
    assert "Job not found" in data["error"]


@pytest.mark.django_db(transaction=True)
async def test_list_repositories_unauthenticated_rejected():
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=None)):
        result = await list_repositories()
    assert "error" in result
