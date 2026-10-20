from dataclasses import replace

from django.db import transaction

import pytest
from django_tasks import TaskResultStatus, task
from django_tasks.utils import normalize_json
from django_tasks_db.models import DBTaskResult


@task(dedup=True)
def sample_issue_task(repo_id: str, issue_iid: int, *, priority: str = "normal") -> str:
    return f"{repo_id}:{issue_iid}:{priority}"


@pytest.mark.django_db
def test_dedup_backend_skips_duplicate_enqueue_for_matching_args(database_task_backend):
    result = sample_issue_task.enqueue("repo-1", 99, priority="high")
    duplicate_result = sample_issue_task.enqueue("repo-1", 99, priority="high")

    assert result.id == duplicate_result.id
    assert DBTaskResult.objects.filter(task_path=sample_issue_task.module_path).count() == 1

    db_result = DBTaskResult.objects.get(id=result.id)
    assert db_result.args_kwargs == normalize_json({"args": ["repo-1", 99], "kwargs": {"priority": "high"}})


@pytest.mark.django_db
def test_dedup_backend_reuses_task_after_success(database_task_backend):
    result = sample_issue_task.enqueue("repo-1", 42)
    db_result = DBTaskResult.objects.get(id=result.id)
    db_result.set_successful(return_value=None)

    second_result = sample_issue_task.enqueue("repo-1", 42)

    assert result.id == second_result.id
    assert DBTaskResult.objects.filter(task_path=sample_issue_task.module_path).count() == 1


@pytest.mark.django_db
def test_dedup_backend_allows_new_after_failure(database_task_backend):
    result = sample_issue_task.enqueue("repo-1", 100)
    db_result = DBTaskResult.objects.get(id=result.id)
    db_result.set_failed(RuntimeError("boom"))

    second_result = sample_issue_task.enqueue("repo-1", 100)

    assert result.id != second_result.id
    assert DBTaskResult.objects.filter(task_path=sample_issue_task.module_path).count() == 2


@pytest.mark.django_db
def test_dedup_backend_creates_new_for_different_args(database_task_backend):
    result = sample_issue_task.enqueue("repo-1", 10, priority="high")
    second_result = sample_issue_task.enqueue("repo-1", 11, priority="high")

    assert result.id != second_result.id
    assert DBTaskResult.objects.filter(task_path=sample_issue_task.module_path).count() == 2


@task
def sample_cron_task(repo_id: str) -> str:
    return repo_id


@pytest.mark.django_db(transaction=True)
async def test_dedup_backend_skips_duplicate_aenqueue(database_task_backend):
    result = await sample_issue_task.aenqueue("repo-1", 7)
    await DBTaskResult.objects.filter(id=result.id).aupdate(status=TaskResultStatus.SUCCESSFUL)

    duplicate_result = await sample_issue_task.aenqueue("repo-1", 7)

    assert result.id == duplicate_result.id
    assert await DBTaskResult.objects.filter(task_path=sample_issue_task.module_path).acount() == 1


@pytest.mark.django_db(transaction=True)
async def test_aenqueue_without_dedup_always_creates_a_task(database_task_backend):
    result = await sample_cron_task.aenqueue("repo-1")
    second_result = await sample_cron_task.aenqueue("repo-1")

    assert result.id != second_result.id


@task(dedup=True, dedup_on=("repo_id", "note_id"))
def sample_mention_task(repo_id: str, note_id: int | None = None, use_max: bool = False) -> str:
    return repo_id


@pytest.mark.django_db
def test_dedup_on_ignores_the_arguments_it_does_not_name(database_task_backend):
    result = sample_mention_task.enqueue(repo_id="repo-1", note_id=5, use_max=False)

    assert sample_mention_task.enqueue(repo_id="repo-1", note_id=5, use_max=True).id == result.id
    assert sample_mention_task.enqueue(repo_id="repo-1", note_id=6).id != result.id
    assert sample_mention_task.enqueue(repo_id="repo-1").id != result.id
    assert sample_mention_task.enqueue(repo_id="repo-1").id == sample_mention_task.enqueue(repo_id="repo-1").id


@pytest.mark.django_db
def test_dedup_on_rejects_positional_arguments(database_task_backend):
    with pytest.raises(TypeError, match="by keyword"):
        sample_mention_task.enqueue("repo-1", 5)


@pytest.mark.django_db
def test_without_dedup_successful_only_a_queued_or_running_task_is_a_duplicate(database_task_backend):
    active_only = replace(sample_mention_task, dedup_successful=False)
    result = active_only.enqueue(repo_id="repo-1", note_id=5)
    assert active_only.enqueue(repo_id="repo-1", note_id=5).id == result.id

    DBTaskResult.objects.get(id=result.id).set_successful(return_value=None)

    assert active_only.enqueue(repo_id="repo-1", note_id=5).id != result.id


@pytest.mark.django_db
def test_get_or_enqueue_says_whether_it_created_the_task(database_task_backend):
    result, created = sample_mention_task.get_or_enqueue(repo_id="repo-1", note_id=5)
    duplicate, duplicate_created = sample_mention_task.get_or_enqueue(repo_id="repo-1", note_id=5, use_max=True)

    assert (created, duplicate_created) == (True, False)
    assert duplicate.id == result.id


@pytest.mark.django_db
def test_get_or_enqueue_without_dedup_always_creates(database_task_backend):
    first, second = sample_cron_task.get_or_enqueue("repo-1"), sample_cron_task.get_or_enqueue("repo-1")

    assert (first[1], second[1]) == (True, True)
    assert first[0].id != second[0].id


@pytest.mark.django_db
def test_a_task_enqueued_in_a_rolled_back_transaction_is_no_duplicate(database_task_backend):
    with pytest.raises(RuntimeError), transaction.atomic():
        sample_mention_task.get_or_enqueue(repo_id="repo-1", note_id=5)
        raise RuntimeError

    assert sample_mention_task.get_or_enqueue(repo_id="repo-1", note_id=5)[1] is True


def test_the_immediate_backend_runs_and_creates_every_time():
    result, created = sample_mention_task.get_or_enqueue(repo_id="repo-1", note_id=5)

    assert created is True
    assert result.return_value == "repo-1"
