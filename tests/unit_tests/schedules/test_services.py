from datetime import time as dt_time

from django.core.exceptions import ValidationError

import pytest

from accounts.models import User
from schedules.models import Frequency
from schedules.services import acreate_scheduled_job, alist_scheduled_jobs


async def _user(username):
    return await User.objects.acreate_user(username=username, email=f"{username}@e.com", password="x")  # noqa: S106


@pytest.mark.django_db(transaction=True)
async def test_acreate_scheduled_job_daily_computes_next_run():
    user = await _user("s1")
    job = await acreate_scheduled_job(
        user,
        name="Nightly",
        prompt="do it",
        repos=[{"repo_id": "a/b", "ref": ""}],
        frequency=Frequency.DAILY,
        time=dt_time(9, 0),
    )
    assert job.pk is not None
    assert job.user_id == user.pk
    assert job.is_enabled is True
    assert job.next_run_at is not None


@pytest.mark.django_db(transaction=True)
async def test_acreate_scheduled_job_daily_requires_time():
    user = await _user("s2")
    with pytest.raises(ValidationError):
        await acreate_scheduled_job(
            user, name="x", prompt="p", repos=[{"repo_id": "a/b", "ref": ""}], frequency=Frequency.DAILY, time=None
        )


@pytest.mark.django_db(transaction=True)
async def test_acreate_scheduled_job_custom_requires_cron():
    user = await _user("s3")
    with pytest.raises(ValidationError):
        await acreate_scheduled_job(
            user,
            name="x",
            prompt="p",
            repos=[{"repo_id": "a/b", "ref": ""}],
            frequency=Frequency.CUSTOM,
            cron_expression="",
        )


@pytest.mark.django_db(transaction=True)
async def test_alist_scheduled_jobs_scopes_and_filters():
    user = await _user("s4")
    other = await _user("s4b")
    await acreate_scheduled_job(
        user,
        name="mine",
        prompt="p",
        repos=[{"repo_id": "a/b", "ref": ""}],
        frequency=Frequency.DAILY,
        time=dt_time(8, 0),
    )
    await acreate_scheduled_job(
        other,
        name="theirs",
        prompt="p",
        repos=[{"repo_id": "a/b", "ref": ""}],
        frequency=Frequency.DAILY,
        time=dt_time(8, 0),
    )
    mine = await alist_scheduled_jobs(user)
    assert [s.name for s in mine] == ["mine"]
    by_repo = await alist_scheduled_jobs(user, repo_id="a/b")
    assert len(by_repo) == 1
    none = await alist_scheduled_jobs(user, repo_id="z/z")
    assert none == []


@pytest.mark.django_db(transaction=True)
async def test_alist_scheduled_jobs_enabled_only_excludes_disabled():
    user = await _user("s5")
    await acreate_scheduled_job(
        user,
        name="on",
        prompt="p",
        repos=[{"repo_id": "a/b", "ref": ""}],
        frequency=Frequency.DAILY,
        time=dt_time(8, 0),
    )
    disabled = await acreate_scheduled_job(
        user,
        name="off",
        prompt="p",
        repos=[{"repo_id": "a/b", "ref": ""}],
        frequency=Frequency.DAILY,
        time=dt_time(8, 0),
    )
    disabled.is_enabled = False
    await disabled.asave(update_fields=["is_enabled"])

    all_jobs = await alist_scheduled_jobs(user)
    only_enabled = await alist_scheduled_jobs(user, enabled_only=True)

    assert {s.name for s in all_jobs} == {"on", "off"}
    assert {s.name for s in only_enabled} == {"on"}
