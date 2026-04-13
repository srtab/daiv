from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from activity.models import Activity, ActivityStatus, TriggerType

from accounts.models import User


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="testpass",  # noqa: S106
        role="admin",
    )


@pytest.fixture
def member_user(db):
    return User.objects.create_user(
        username="member",
        email="member@test.com",
        password="testpass",  # noqa: S106
        role="member",
    )


def _create_activity(user=None, external_username=""):
    return Activity.objects.create(
        trigger_type=TriggerType.ISSUE_WEBHOOK, repo_id="group/repo", user=user, external_username=external_username
    )


class TestByOwner:
    def test_admin_sees_all_activities(self, admin_user, member_user):
        a1 = _create_activity(user=admin_user)
        a2 = _create_activity(user=member_user)
        a3 = _create_activity(external_username="someone_else")

        qs = Activity.objects.by_owner(admin_user)
        assert set(qs.values_list("pk", flat=True)) == {a1.pk, a2.pk, a3.pk}

    def test_member_sees_own_activities(self, member_user):
        own = _create_activity(user=member_user)
        _create_activity(external_username="other")

        qs = Activity.objects.by_owner(member_user)
        assert list(qs.values_list("pk", flat=True)) == [own.pk]

    def test_member_sees_activities_by_external_username(self, member_user):
        by_fk = _create_activity(user=member_user)
        by_ext = _create_activity(external_username="member")
        _create_activity(external_username="someone_else")

        qs = Activity.objects.by_owner(member_user)
        assert set(qs.values_list("pk", flat=True)) == {by_fk.pk, by_ext.pk}

    def test_member_sees_orphaned_activities_before_backfill(self, db):
        """Activities with external_username but no user FK should be visible after login."""
        orphan = _create_activity(external_username="newdev")
        _create_activity(external_username="other")

        user = User.objects.create_user(
            username="newdev",
            email="newdev@test.com",
            password="testpass",  # noqa: S106
        )

        qs = Activity.objects.by_owner(user)
        assert orphan.pk in set(qs.values_list("pk", flat=True))


@pytest.mark.django_db
class TestSyncAndSave:
    def test_returns_true_and_persists_when_changed(self, create_db_task_result):
        finished = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)
        tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "Job done."}, finished_at=finished)
        activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB, repo_id="group/project", status=ActivityStatus.READY, task_result=tr
        )

        assert activity.sync_and_save() is True

        activity.refresh_from_db()
        assert activity.status == ActivityStatus.SUCCESSFUL
        assert activity.finished_at == finished
        assert activity.result_summary == "Job done."

    def test_returns_false_and_skips_save_when_no_changes(self, create_db_task_result):
        finished = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)
        tr = create_db_task_result(
            status="SUCCESSFUL", return_value={"response": "Already synced."}, finished_at=finished
        )
        activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            repo_id="group/project",
            status=ActivityStatus.SUCCESSFUL,
            task_result=tr,
            finished_at=finished,
            result_summary="Already synced.",
        )

        with patch.object(Activity, "save") as mock_save:
            assert activity.sync_and_save() is False

        mock_save.assert_not_called()
