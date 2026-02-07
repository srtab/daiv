import pytest
from django_tasks import task
from django_tasks.utils import normalize_json
from django_tasks_db.models import DBTaskResult


@task
def sample_issue_task(repo_id: str, issue_iid: int, *, priority: str = "normal") -> str:
    return f"{repo_id}:{issue_iid}:{priority}"


@pytest.fixture
def dedup_backend(settings):
    settings.TASKS = {
        "default": {"BACKEND": "core.backends.deduplicating.DeduplicatingDatabaseBackend", "QUEUES": ["default"]}
    }

    from django.tasks import task_backends

    task_backends._settings = None
    task_backends._connections = type(task_backends._connections)()
    return task_backends["default"]


@pytest.mark.django_db
def test_dedup_backend_skips_duplicate_enqueue_for_matching_args(dedup_backend):
    result = sample_issue_task.enqueue("repo-1", 99, priority="high")
    duplicate_result = sample_issue_task.enqueue("repo-1", 99, priority="high")

    assert result.id == duplicate_result.id
    assert DBTaskResult.objects.filter(task_path=sample_issue_task.module_path).count() == 1

    db_result = DBTaskResult.objects.get(id=result.id)
    assert db_result.args_kwargs == normalize_json({"args": ["repo-1", 99], "kwargs": {"priority": "high"}})


@pytest.mark.django_db
def test_dedup_backend_reuses_task_after_success(dedup_backend):
    result = sample_issue_task.enqueue("repo-1", 42)
    db_result = DBTaskResult.objects.get(id=result.id)
    db_result.set_successful(return_value=None)

    second_result = sample_issue_task.enqueue("repo-1", 42)

    assert result.id == second_result.id
    assert DBTaskResult.objects.filter(task_path=sample_issue_task.module_path).count() == 1


@pytest.mark.django_db
def test_dedup_backend_allows_new_after_failure(dedup_backend):
    result = sample_issue_task.enqueue("repo-1", 100)
    db_result = DBTaskResult.objects.get(id=result.id)
    db_result.set_failed(RuntimeError("boom"))

    second_result = sample_issue_task.enqueue("repo-1", 100)

    assert result.id != second_result.id
    assert DBTaskResult.objects.filter(task_path=sample_issue_task.module_path).count() == 2


@pytest.mark.django_db
def test_dedup_backend_creates_new_for_different_args(dedup_backend):
    result = sample_issue_task.enqueue("repo-1", 10, priority="high")
    second_result = sample_issue_task.enqueue("repo-1", 11, priority="high")

    assert result.id != second_result.id
    assert DBTaskResult.objects.filter(task_path=sample_issue_task.module_path).count() == 2
