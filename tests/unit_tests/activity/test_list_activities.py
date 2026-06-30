import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from activity.services import alist_user_activities

from accounts.models import User


async def _user(username):
    return await User.objects.acreate_user(username=username, email=f"{username}@e.com", password="x")  # noqa: S106


async def _activity(user, repo_id="a/b", status=ActivityStatus.SUCCESSFUL):
    return await Activity.objects.acreate(user=user, repo_id=repo_id, status=status, trigger_type=TriggerType.MCP_JOB)


@pytest.mark.django_db(transaction=True)
async def test_alist_user_activities_scopes_to_user():
    user = await _user("u1")
    other = await _user("o1")
    await _activity(user)
    await _activity(other)
    rows = await alist_user_activities(user)
    assert len(rows) == 1
    assert rows[0].user_id == user.pk


@pytest.mark.django_db(transaction=True)
async def test_alist_user_activities_filters_repo_and_status():
    user = await _user("u2")
    await _activity(user, repo_id="a/b", status=ActivityStatus.SUCCESSFUL)
    await _activity(user, repo_id="c/d", status=ActivityStatus.RUNNING)
    by_repo = await alist_user_activities(user, repo_id="a/b")
    assert {r.repo_id for r in by_repo} == {"a/b"}
    by_status = await alist_user_activities(user, status=ActivityStatus.RUNNING)
    assert {r.status for r in by_status} == {ActivityStatus.RUNNING}


@pytest.mark.django_db(transaction=True)
async def test_alist_user_activities_respects_limit():
    user = await _user("u3")
    for _ in range(3):
        await _activity(user)
    rows = await alist_user_activities(user, limit=2)
    assert len(rows) == 2
