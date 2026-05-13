from datetime import UTC, datetime, time

import pytest
from activity.filters import ActivityFilter
from activity.models import Activity, ActivityStatus, TriggerType

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="alice",
        email="alice@test.com",
        password="testpass123",  # noqa: S106
    )


def _create(**kwargs):
    defaults = {
        "trigger_type": TriggerType.SCHEDULE,
        "repo_id": "group/project",
        "ref": "main",
        "status": ActivityStatus.SUCCESSFUL,
    }
    defaults.update(kwargs)
    return Activity.objects.create(**defaults)


@pytest.mark.django_db
class TestActivityFilter:
    def test_no_params_returns_all(self, user):
        a = _create()
        b = _create(status=ActivityStatus.FAILED)
        qs = ActivityFilter({}, queryset=Activity.objects.all()).qs
        assert a in qs
        assert b in qs

    def test_status_filter(self, user):
        successful = _create(status=ActivityStatus.SUCCESSFUL)
        failed = _create(status=ActivityStatus.FAILED)
        qs = ActivityFilter({"status": ActivityStatus.SUCCESSFUL}, queryset=Activity.objects.all()).qs
        assert successful in qs
        assert failed not in qs

    def test_invalid_status_is_ignored(self, user):
        a = _create()
        f = ActivityFilter({"status": "bogus"}, queryset=Activity.objects.all())
        assert not f.form.is_valid()
        # Invalid choice is dropped from cleaned_data → no filter applied for that field.
        assert a in f.qs

    def test_trigger_filter(self, user):
        sched = _create(trigger_type=TriggerType.SCHEDULE)
        webhook = _create(trigger_type=TriggerType.ISSUE_WEBHOOK)
        qs = ActivityFilter({"trigger": TriggerType.SCHEDULE}, queryset=Activity.objects.all()).qs
        assert sched in qs
        assert webhook not in qs

    def test_repo_filter(self, user):
        a = _create(repo_id="group/project")
        b = _create(repo_id="group/other")
        qs = ActivityFilter({"repo": "group/project"}, queryset=Activity.objects.all()).qs
        assert a in qs
        assert b not in qs

    def test_schedule_filter_accepts_int_string(self, user):
        a = _create()
        f = ActivityFilter({"schedule": "not-a-number"}, queryset=Activity.objects.all())
        assert not f.form.is_valid()
        # Invalid int is dropped from cleaned_data.
        assert a in f.qs

    def test_schedule_filter_matches_fk(self, user):
        job = ScheduledJob.objects.create(
            user=user,
            name="nightly",
            prompt="x",
            repos=[{"repo_id": "group/project", "ref": ""}],
            frequency=Frequency.DAILY,
            time=time(3, 0),
        )
        match = _create(scheduled_job=job)
        other = _create()
        qs = ActivityFilter({"schedule": str(job.pk)}, queryset=Activity.objects.all()).qs
        assert match in qs
        assert other not in qs

    def test_date_from_filter(self, user):
        old = _create()
        Activity.objects.filter(pk=old.pk).update(created_at=datetime(2020, 1, 1, tzinfo=UTC))
        recent = _create()
        Activity.objects.filter(pk=recent.pk).update(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        qs = ActivityFilter({"date_from": "2025-06-01"}, queryset=Activity.objects.all()).qs
        assert recent in qs
        assert old not in qs

    def test_date_to_filter(self, user):
        old = _create()
        Activity.objects.filter(pk=old.pk).update(created_at=datetime(2020, 1, 1, tzinfo=UTC))
        recent = _create()
        Activity.objects.filter(pk=recent.pk).update(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        qs = ActivityFilter({"date_to": "2025-06-01"}, queryset=Activity.objects.all()).qs
        assert old in qs
        assert recent not in qs

    def test_date_range_combined(self, user):
        before = _create()
        Activity.objects.filter(pk=before.pk).update(created_at=datetime(2020, 1, 1, tzinfo=UTC))
        inside = _create()
        Activity.objects.filter(pk=inside.pk).update(created_at=datetime(2025, 6, 15, tzinfo=UTC))
        after = _create()
        Activity.objects.filter(pk=after.pk).update(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        qs = ActivityFilter({"date_from": "2025-01-01", "date_to": "2025-12-31"}, queryset=Activity.objects.all()).qs
        assert inside in qs
        assert before not in qs
        assert after not in qs

    def test_invalid_date_is_ignored(self, user):
        a = _create()
        f = ActivityFilter({"date_from": "not-a-date"}, queryset=Activity.objects.all())
        assert not f.form.is_valid()
        # Invalid date is dropped from cleaned_data → no filter applied.
        assert a in f.qs

    def test_combined_filters(self, user):
        match = _create(status=ActivityStatus.SUCCESSFUL, repo_id="group/project")
        wrong_status = _create(status=ActivityStatus.FAILED, repo_id="group/project")
        wrong_repo = _create(status=ActivityStatus.SUCCESSFUL, repo_id="group/other")
        qs = ActivityFilter(
            {"status": ActivityStatus.SUCCESSFUL, "repo": "group/project"}, queryset=Activity.objects.all()
        ).qs
        assert match in qs
        assert wrong_status not in qs
        assert wrong_repo not in qs

    def test_batch_filter_matches_by_batch_id(self, user):
        import uuid as _uuid

        b = _uuid.uuid4()
        match = _create(batch_id=b)
        other = _create(batch_id=_uuid.uuid4())
        qs = ActivityFilter({"batch": str(b)}, queryset=Activity.objects.all()).qs
        assert match in qs
        assert other not in qs

    def test_batch_filter_invalid_uuid_is_ignored(self, user):
        a = _create()
        f = ActivityFilter({"batch": "not-a-uuid"}, queryset=Activity.objects.all())
        assert not f.form.is_valid()
        # Invalid value is dropped → no filter applied.
        assert a in f.qs
