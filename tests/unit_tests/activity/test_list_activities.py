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


@pytest.mark.django_db(transaction=True)
async def test_alist_user_activities_before_returns_only_older_rows():
    """The keyset ``before`` predicate returns rows strictly older than the cursor row,
    in ``-created_at, -id`` order."""
    from datetime import timedelta

    from django.utils import timezone

    user = await _user("u4")
    now = timezone.now()
    rows_in = []
    for _ in range(4):
        rows_in.append(await _activity(user))
    for offset, act in enumerate(rows_in):
        await Activity.objects.filter(pk=act.pk).aupdate(created_at=now - timedelta(minutes=offset))
    # rows_in[0] is newest. First page (limit 2) → [rows_in[0], rows_in[1]].
    page1 = await alist_user_activities(user, limit=2)
    assert [r.id for r in page1] == [rows_in[0].id, rows_in[1].id]
    # Resume after page1's last row → the two older rows.
    page2 = await alist_user_activities(user, limit=2, before=(page1[-1].created_at, page1[-1].id))
    assert [r.id for r in page2] == [rows_in[2].id, rows_in[3].id]


@pytest.mark.django_db(transaction=True)
async def test_alist_user_activities_before_tie_break_on_equal_created_at():
    """When several rows share an identical created_at, the id tie-break keeps the cursor
    unambiguous — no row is skipped or repeated across pages."""
    from django.utils import timezone

    user = await _user("u5")
    same = timezone.now()
    created = []
    for _ in range(4):
        created.append(await _activity(user))
    for act in created:
        await Activity.objects.filter(pk=act.pk).aupdate(created_at=same)

    collected = []
    cursor = None
    for _ in range(10):
        page = await alist_user_activities(user, limit=2, before=cursor)
        if not page:
            break
        collected.extend(page)
        cursor = (page[-1].created_at, page[-1].id)

    ids = [r.id for r in collected]
    assert sorted(str(i) for i in ids) == sorted(str(a.id) for a in created)
    assert len(ids) == len(set(ids))
