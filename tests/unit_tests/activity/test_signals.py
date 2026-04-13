import pytest
from activity.models import Activity, TriggerType

from accounts.models import User


@pytest.mark.django_db
class TestBackfillActivityUser:
    def test_backfills_orphaned_activities_on_user_create(self):
        orphan = Activity.objects.create(
            trigger_type=TriggerType.ISSUE_WEBHOOK, repo_id="group/repo", external_username="newdev"
        )
        assert orphan.user is None

        user = User.objects.create_user(
            username="newdev",
            email="newdev@test.com",
            password="testpass",  # noqa: S106
        )

        orphan.refresh_from_db()
        assert orphan.user == user

    def test_does_not_backfill_already_linked_activities(self):
        existing_user = User.objects.create_user(
            username="existing",
            email="existing@test.com",
            password="testpass",  # noqa: S106
        )
        linked = Activity.objects.create(
            trigger_type=TriggerType.ISSUE_WEBHOOK, repo_id="group/repo", user=existing_user, external_username="newdev"
        )

        new_user = User.objects.create_user(
            username="newdev",
            email="newdev@test.com",
            password="testpass",  # noqa: S106
        )

        linked.refresh_from_db()
        assert linked.user == existing_user, "Should not overwrite existing user FK"
        assert linked.user != new_user

    def test_does_not_backfill_on_user_update(self):
        orphan = Activity.objects.create(
            trigger_type=TriggerType.ISSUE_WEBHOOK, repo_id="group/repo", external_username="devuser"
        )

        user = User.objects.create_user(
            username="devuser",
            email="dev@test.com",
            password="testpass",  # noqa: S106
        )

        orphan.refresh_from_db()
        assert orphan.user == user

        # Now unlink manually and update user — should NOT re-backfill
        Activity.objects.filter(pk=orphan.pk).update(user=None)
        user.name = "Updated Name"
        user.save()

        orphan.refresh_from_db()
        assert orphan.user is None, "Should not backfill on user update, only on create"

    def test_no_match_when_external_username_differs(self):
        orphan = Activity.objects.create(
            trigger_type=TriggerType.ISSUE_WEBHOOK, repo_id="group/repo", external_username="other_user"
        )

        User.objects.create_user(
            username="newdev",
            email="newdev@test.com",
            password="testpass",  # noqa: S106
        )

        orphan.refresh_from_db()
        assert orphan.user is None
