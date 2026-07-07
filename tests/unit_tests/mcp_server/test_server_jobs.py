import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import User


async def _user(username):
    return await User.objects.acreate_user(username=username, email=f"{username}@e.com", password="x")  # noqa: S106


async def _session(user, *, repo_id="a/b", thread_id=None):
    return await Session.objects.acreate(
        thread_id=thread_id or str(uuid.uuid4()), origin=SessionOrigin.MCP_JOB, user=user, repo_id=repo_id
    )


async def _run(session, *, status=RunStatus.QUEUED, **kwargs):
    return await Run.objects.acreate(
        session=session,
        user=session.user,
        repo_id=session.repo_id,
        trigger_type=SessionOrigin.MCP_JOB,
        status=status,
        **kwargs,
    )


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_returns_user_jobs_and_excludes_result_summary():
    from mcp_server.server import list_jobs

    user = await _user("lj1")
    sess = await _session(user)
    await _run(sess, status=RunStatus.SUCCESSFUL, result_summary="secret detail")
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs()
    assert data["next_cursor"] is None
    assert len(data["jobs"]) == 1
    job = data["jobs"][0]
    assert job["repo_id"] == "a/b"
    assert job["status"] == "SUCCESSFUL"
    assert "result_summary" not in job


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_truncates_and_caps():
    from mcp_server.server import list_jobs

    user = await _user("lj2")
    sess = await _session(user)
    for _ in range(3):
        await _run(sess)
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs(limit=2)
    assert len(data["jobs"]) == 2
    assert data["next_cursor"] is not None


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_cursor_paginates_without_overlap():
    """Walking pages via next_cursor covers every row exactly once, newest first."""
    from mcp_server.server import list_jobs

    user = await _user("lj_pg")
    now = timezone.now()
    sess = await _session(user)
    created = []
    for _ in range(5):
        created.append(await _run(sess))
    # Give each a distinct created_at so ordering is deterministic. created[0] gets the
    # latest timestamp (now - 0min), so created is already in newest-first order.
    for offset, run in enumerate(created):
        await Run.objects.filter(pk=run.pk).aupdate(created_at=now - timedelta(minutes=offset))
    expected = [str(r.id) for r in created]  # newest first

    seen: list[str] = []
    cursor = None
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        for _ in range(10):  # generous upper bound; loop should break well before
            data = await list_jobs(limit=2, cursor=cursor)
            seen.extend(j["job_id"] for j in data["jobs"])
            cursor = data["next_cursor"]
            if cursor is None:
                break

    assert cursor is None
    assert seen == expected  # no gaps, no repeats, correct order


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_cursor_tie_break_on_same_created_at():
    """Rows sharing an identical created_at (batch submit) must not be skipped or repeated."""
    from mcp_server.server import list_jobs

    user = await _user("lj_tie")
    same = timezone.now()
    sess = await _session(user)
    created = []
    for _ in range(4):
        created.append(await _run(sess))
    for run in created:
        await Run.objects.filter(pk=run.pk).aupdate(created_at=same)

    seen: list[str] = []
    cursor = None
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        for _ in range(10):
            data = await list_jobs(limit=2, cursor=cursor)
            seen.extend(j["job_id"] for j in data["jobs"])
            cursor = data["next_cursor"]
            if cursor is None:
                break

    assert sorted(seen) == sorted(str(r.id) for r in created)
    assert len(seen) == len(set(seen))  # each row exactly once


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_invalid_cursor_returns_error():
    from mcp_server.server import list_jobs

    user = await _user("lj_badc")
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs(cursor="not-a-valid-cursor")
    assert "error" in data
    assert "cursor" in data["error"].lower()


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_wrong_tool_or_bad_id_cursor_returns_invalid():
    """A decodable cursor whose id can't coerce to Run's UUID PK (e.g. a schedules
    cursor with an integer id, or plain junk) must be reported as "Invalid cursor." at decode
    time — not deferred to the ORM where the generic handler mislabels it as transient."""
    from mcp_server.server import _encode_cursor, list_jobs

    user = await _user("lj_xtool")
    bad = _encode_cursor({"c": timezone.now().isoformat(), "id": "5"})  # int id, as schedules emits
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs(cursor=bad)
    assert data.get("error") == "Invalid cursor."


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_unauthenticated_returns_error():
    from mcp_server.server import list_jobs

    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=None)):
        data = await list_jobs()
    assert "error" in data


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_orders_newest_first():
    from mcp_server.server import list_jobs

    user = await _user("lj3")
    sess = await _session(user)
    older = await _run(sess)
    newer = await _run(sess)
    # created_at is auto_now_add, so nudge it explicitly to make ordering observable.
    now = timezone.now()
    await Run.objects.filter(pk=older.pk).aupdate(created_at=now - timedelta(hours=1))
    await Run.objects.filter(pk=newer.pk).aupdate(created_at=now)
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs()
    assert [j["job_id"] for j in data["jobs"]] == [str(newer.id), str(older.id)]


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_not_truncated_at_exact_limit():
    from mcp_server.server import list_jobs

    user = await _user("lj4")
    sess = await _session(user)
    for _ in range(2):
        await _run(sess)
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs(limit=2)
    assert len(data["jobs"]) == 2
    assert data["next_cursor"] is None


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_serializes_cost_and_tokens():
    from mcp_server.server import list_jobs

    user = await _user("lj5")
    sess = await _session(user)
    await _run(sess, status=RunStatus.SUCCESSFUL, cost_usd=Decimal("1.234567"), total_tokens=4242)
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs()
    job = data["jobs"][0]
    # cost_usd is stringified (Decimal is not JSON-serializable); total_tokens stays an int.
    assert job["cost_usd"] == "1.234567"
    assert job["total_tokens"] == 4242


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_status_filter():
    from mcp_server.server import list_jobs

    user = await _user("lj6")
    sess = await _session(user)
    await _run(sess, status=RunStatus.RUNNING)
    await _run(sess, status=RunStatus.SUCCESSFUL)
    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        data = await list_jobs(status=RunStatus.RUNNING)
    assert {j["status"] for j in data["jobs"]} == {"RUNNING"}


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_db_error_returns_friendly_error():
    from mcp_server.server import list_jobs

    user = await _user("lj7")
    with (
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)),
        patch("mcp_server.server.alist_user_runs", new=AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        data = await list_jobs()
    assert "error" in data
    assert "db down" not in data["error"]  # internal detail not leaked


@pytest.mark.django_db(transaction=True)
async def test_list_jobs_auth_exception_returns_error():
    from mcp_server.server import list_jobs

    with patch("mcp_server.server.get_current_user", new=AsyncMock(side_effect=RuntimeError("boom"))):
        data = await list_jobs()
    assert "error" in data
