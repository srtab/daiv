from datetime import time as dt_time
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

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
async def test_acreate_scheduled_job_once_computes_next_run_from_run_at():
    user = await _user("s1b")
    run_at = (timezone.now() + timedelta(days=1)).replace(microsecond=0)
    job = await acreate_scheduled_job(
        user,
        name="OneOff",
        prompt="do it",
        repos=[{"repo_id": "a/b", "ref": ""}],
        frequency=Frequency.ONCE,
        run_at=run_at,
    )
    assert job.frequency == Frequency.ONCE
    assert job.next_run_at == run_at


@pytest.mark.django_db(transaction=True)
async def test_acreate_scheduled_job_once_past_run_at_raises_validation_error():
    user = await _user("s1c")
    past = timezone.now() - timedelta(days=1)
    # The model's _validate_frequency_fields rejects this in full_clean() before
    # compute_next_run() runs, so it surfaces as ValidationError (never a bare ValueError).
    with pytest.raises(ValidationError):
        await acreate_scheduled_job(
            user,
            name="OneOff",
            prompt="do it",
            repos=[{"repo_id": "a/b", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=past,
        )


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


async def _daily(user, name, repo_id="a/b"):
    return await acreate_scheduled_job(
        user,
        name=name,
        prompt="p",
        repos=[{"repo_id": repo_id, "ref": ""}],
        frequency=Frequency.DAILY,
        time=dt_time(8, 0),
    )


@pytest.mark.django_db(transaction=True)
async def test_alist_scheduled_jobs_limit_and_before_paginate():
    """``limit`` bounds the page and ``before`` resumes after the cursor row (newest first)."""
    from schedules.models import ScheduledJob

    user = await _user("s6")
    now = timezone.now()
    jobs = [await _daily(user, name) for name in ("A", "B", "C", "D")]
    for offset, job in enumerate(jobs):
        await ScheduledJob.objects.filter(pk=job.pk).aupdate(created=now - timedelta(minutes=offset))
    # jobs[0] ("A") is newest.
    page1 = await alist_scheduled_jobs(user, limit=2)
    assert [s.name for s in page1] == ["A", "B"]
    page2 = await alist_scheduled_jobs(user, limit=2, before=(page1[-1].created, page1[-1].id))
    assert [s.name for s in page2] == ["C", "D"]


@pytest.mark.django_db(transaction=True)
async def test_alist_scheduled_jobs_repo_filter_with_limit_is_not_short_changed():
    """A page must not be short-changed by non-matching rows that sort ahead of matches:
    the Python-side repo_id filter scans past them until ``limit`` matches are collected."""
    from schedules.models import ScheduledJob

    user = await _user("s7")
    now = timezone.now()
    # Interleave matching (x/y) and non-matching (o/t) schedules; the two newest are non-matching.
    layout = [("n0", "o/t"), ("n1", "o/t"), ("m0", "x/y"), ("m1", "x/y"), ("m2", "x/y")]
    for offset, (name, repo) in enumerate(layout):
        job = await _daily(user, name, repo_id=repo)
        await ScheduledJob.objects.filter(pk=job.pk).aupdate(created=now - timedelta(minutes=offset))

    page = await alist_scheduled_jobs(user, repo_id="x/y", limit=2)
    # Must return 2 matching rows even though the 2 newest schedules don't match.
    assert [s.name for s in page] == ["m0", "m1"]
    page2 = await alist_scheduled_jobs(user, repo_id="x/y", limit=2, before=(page[-1].created, page[-1].id))
    assert [s.name for s in page2] == ["m2"]


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
