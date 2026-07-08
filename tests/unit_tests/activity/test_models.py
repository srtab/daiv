import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from allauth.socialaccount.models import SocialAccount

from accounts.models import User
from codebase.base import RepoAccessLevel
from codebase.models import RepositoryAccess
from schedules.models import Frequency, ScheduledJob


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


class TestVisibleTo:
    def test_admin_sees_all_activities(self, admin_user, member_user):
        a1 = _create_activity(user=admin_user)
        a2 = _create_activity(user=member_user)
        a3 = _create_activity(external_username="someone_else")

        qs = Activity.objects.visible_to(admin_user)
        assert set(qs.values_list("pk", flat=True)) == {a1.pk, a2.pk, a3.pk}

    def test_member_sees_own_activities(self, member_user):
        own = _create_activity(user=member_user)
        _create_activity(external_username="other")

        qs = Activity.objects.visible_to(member_user)
        assert list(qs.values_list("pk", flat=True)) == [own.pk]

    def test_member_sees_activities_by_external_username(self, member_user):
        by_fk = _create_activity(user=member_user)
        by_ext = _create_activity(external_username="member")
        _create_activity(external_username="someone_else")

        qs = Activity.objects.visible_to(member_user)
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

        qs = Activity.objects.visible_to(user)
        assert orphan.pk in set(qs.values_list("pk", flat=True))

    def test_member_sees_others_runs_on_readable_repo(self, member_user):
        SocialAccount.objects.create(user=member_user, provider="gitlab", uid="777")
        RepositoryAccess.objects.create(
            provider="gitlab",
            uid="777",
            username="u",
            repo_id="team/repo",
            access_level=RepoAccessLevel.READ,
            synced_at=timezone.now(),
        )
        other = User.objects.create_user(username="bob", email="bob@test.com", password="pw")  # noqa: S106
        theirs = Activity.objects.create(trigger_type=TriggerType.SCHEDULE, repo_id="team/repo", user=other)
        qs = Activity.objects.visible_to(member_user)
        assert theirs.pk in set(qs.values_list("pk", flat=True))

    def test_member_does_not_see_runs_on_unreadable_repo(self, member_user):
        SocialAccount.objects.create(user=member_user, provider="gitlab", uid="777")
        RepositoryAccess.objects.create(
            provider="gitlab",
            uid="777",
            username="u",
            repo_id="team/readable",
            access_level=RepoAccessLevel.READ,
            synced_at=timezone.now(),
        )
        other = User.objects.create_user(username="bob", email="bob@test.com", password="pw")  # noqa: S106
        hidden = Activity.objects.create(trigger_type=TriggerType.SCHEDULE, repo_id="team/secret", user=other)
        qs = Activity.objects.visible_to(member_user)
        assert hidden.pk not in set(qs.values_list("pk", flat=True))

    def test_member_still_sees_own_run_on_unreadable_repo(self, member_user):
        # No access rows for this member: union must still surface their own run (owner FK).
        own = _create_activity(user=member_user)  # repo_id "group/repo", no access grant
        qs = Activity.objects.visible_to(member_user)
        assert own.pk in set(qs.values_list("pk", flat=True))

    def test_deduplicates_run_matching_multiple_branches(self, member_user):
        # A run the member owns, whose scheduled_job has >1 subscriber, matches the owner-FK
        # branch on *every* row of the ``scheduled_job__subscribers`` M2M join. Without
        # ``.distinct()`` the join surfaces the same activity once per subscriber, inflating
        # list rows and dashboard/nav counts. Assert as a list so a duplicate isn't masked.
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repos=[{"repo_id": "group/repo", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
        )
        sub1 = User.objects.create_user(username="sub1", email="sub1@test.com", password="pw")  # noqa: S106
        sub2 = User.objects.create_user(username="sub2", email="sub2@test.com", password="pw")  # noqa: S106
        schedule.subscribers.add(sub1, sub2)
        own = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE, repo_id="group/repo", user=member_user, scheduled_job=schedule
        )
        qs = Activity.objects.visible_to(member_user)
        assert list(qs.values_list("pk", flat=True)) == [own.pk]


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


@pytest.mark.django_db
class TestSyncFromTaskResultUsage:
    def test_syncs_usage_fields_from_successful_result(self, create_db_task_result):
        tr = create_db_task_result(
            status="SUCCESSFUL",
            return_value={
                "response": "Done",
                "code_changes": False,
                "usage": {
                    "input_tokens": 5000,
                    "output_tokens": 2000,
                    "total_tokens": 7000,
                    "cost_usd": "0.033",
                    "by_model": {"claude-sonnet-4-6": {"input_tokens": 5000, "output_tokens": 2000}},
                },
            },
        )
        activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB, repo_id="group/project", status=ActivityStatus.READY, task_result=tr
        )

        changed = activity.sync_from_task_result()
        assert "input_tokens" in changed
        assert "output_tokens" in changed
        assert "total_tokens" in changed
        assert "cost_usd" in changed
        assert "usage_by_model" in changed

        assert activity.input_tokens == 5000
        assert activity.output_tokens == 2000
        assert activity.total_tokens == 7000
        assert activity.cost_usd == Decimal("0.033")
        assert activity.usage_by_model == {"claude-sonnet-4-6": {"input_tokens": 5000, "output_tokens": 2000}}

    def test_no_usage_leaves_fields_null(self, create_db_task_result):
        """Old results without usage field leave Activity usage fields as null."""
        tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "Done"})
        activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB, repo_id="group/project", status=ActivityStatus.READY, task_result=tr
        )

        changed = activity.sync_from_task_result()
        assert "input_tokens" not in changed
        assert activity.input_tokens is None
        assert activity.cost_usd is None

    def test_usage_not_overwritten_on_re_sync(self, create_db_task_result):
        """Once usage is synced, re-syncing doesn't overwrite."""
        tr = create_db_task_result(
            status="SUCCESSFUL",
            return_value={
                "response": "Done",
                "usage": {
                    "input_tokens": 5000,
                    "output_tokens": 2000,
                    "total_tokens": 7000,
                    "cost_usd": "0.033",
                    "by_model": {},
                },
            },
        )
        activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            repo_id="group/project",
            status=ActivityStatus.SUCCESSFUL,
            task_result=tr,
            result_summary="Done",
            input_tokens=5000,
            output_tokens=2000,
            total_tokens=7000,
            cost_usd=Decimal("0.033"),
            usage_by_model={},
        )

        changed = activity.sync_from_task_result()
        assert "input_tokens" not in changed

    def test_syncs_tokens_when_cost_is_null(self, create_db_task_result):
        """When cost_usd is None (unknown model), tokens are still synced."""
        tr = create_db_task_result(
            status="SUCCESSFUL",
            return_value={
                "response": "Done",
                "usage": {
                    "input_tokens": 3000,
                    "output_tokens": 1000,
                    "total_tokens": 4000,
                    "cost_usd": None,
                    "by_model": {},
                },
            },
        )
        activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB, repo_id="group/project", status=ActivityStatus.READY, task_result=tr
        )

        changed = activity.sync_from_task_result()
        assert "input_tokens" in changed
        assert activity.input_tokens == 3000
        assert activity.cost_usd is None
        assert "cost_usd" not in changed


@pytest.mark.django_db
class TestActivityBatchId:
    def test_batch_id_persisted(self, member_user):
        batch = uuid.uuid4()
        activity = Activity.objects.create(
            trigger_type=TriggerType.UI_JOB, repo_id="x/y", user=member_user, batch_id=batch
        )
        activity.refresh_from_db()
        assert activity.batch_id == batch

    def test_batch_id_defaults_to_null(self, member_user):
        activity = Activity.objects.create(trigger_type=TriggerType.UI_JOB, repo_id="x/y", user=member_user)
        assert activity.batch_id is None

    def test_by_batch_returns_only_matching(self, member_user):
        b1, b2 = uuid.uuid4(), uuid.uuid4()
        a = Activity.objects.create(trigger_type=TriggerType.UI_JOB, repo_id="x/y", user=member_user, batch_id=b1)
        other = Activity.objects.create(trigger_type=TriggerType.UI_JOB, repo_id="x/y", user=member_user, batch_id=b2)
        qs = Activity.objects.by_batch(b1)
        assert a in qs
        assert other not in qs


class TestActivityThreadId:
    def test_duplicate_thread_id_allowed(self, member_user):
        """The same deterministic thread_id is reused across webhook events on a single MR/issue,
        so multiple Activity rows must be allowed to share it."""
        shared = "deadbeef" * 4
        first = Activity.objects.create(
            trigger_type=TriggerType.MR_WEBHOOK,
            repo_id="group/repo",
            user=member_user,
            thread_id=shared,
            mention_comment_id="100",
        )
        second = Activity.objects.create(
            trigger_type=TriggerType.MR_WEBHOOK,
            repo_id="group/repo",
            user=member_user,
            thread_id=shared,
            mention_comment_id="200",
        )
        assert first.pk != second.pk
        assert first.thread_id == second.thread_id == shared

    def test_empty_string_thread_id_rejected(self, member_user):
        """The non-empty CheckConstraint still applies after dropping uniqueness."""
        from django.db.utils import IntegrityError

        with pytest.raises(IntegrityError):
            Activity.objects.create(trigger_type=TriggerType.UI_JOB, repo_id="x/y", user=member_user, thread_id="")


class TestQueuedStatus:
    def test_queued_in_choices(self):
        assert ActivityStatus.QUEUED == "QUEUED"
        assert "QUEUED" in {s.value for s in ActivityStatus}

    def test_queued_is_not_terminal(self):
        assert "QUEUED" not in ActivityStatus.terminal()


@pytest.mark.django_db(transaction=True)
class TestActiveThreadConstraint:
    """Sentinel tests pinning the partial unique constraint ``activity_one_active_per_thread``.

    A future migration that drops the constraint, removes the partial filter, or widens its
    trigger_type scope would silently re-open the TOCTOU race that the constraint exists to
    close — these tests fail loudly in that case.
    """

    def test_two_active_api_rows_on_same_thread_violate(self, member_user):
        from django.db.utils import IntegrityError

        thread = str(uuid.uuid4())
        Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            repo_id="a/b",
            user=member_user,
            thread_id=thread,
            status=ActivityStatus.READY,
        )
        with pytest.raises(IntegrityError):
            Activity.objects.create(
                trigger_type=TriggerType.API_JOB,
                repo_id="a/b",
                user=member_user,
                thread_id=thread,
                status=ActivityStatus.RUNNING,
            )

    def test_two_active_schedule_rows_on_same_thread_allowed(self, member_user):
        """SCHEDULE rows are intentionally excluded from the constraint."""
        thread = str(uuid.uuid4())
        Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            repo_id="a/b",
            user=member_user,
            thread_id=thread,
            status=ActivityStatus.RUNNING,
        )
        Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            repo_id="a/b",
            user=member_user,
            thread_id=thread,
            status=ActivityStatus.READY,
        )

    def test_multiple_queued_siblings_allowed(self, member_user):
        """QUEUED is intentionally outside the constraint so siblings can stack FIFO."""
        thread = str(uuid.uuid4())
        Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            repo_id="a/b",
            user=member_user,
            thread_id=thread,
            status=ActivityStatus.RUNNING,
        )
        Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            repo_id="a/b",
            user=member_user,
            thread_id=thread,
            status=ActivityStatus.QUEUED,
        )
        Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            repo_id="a/b",
            user=member_user,
            thread_id=thread,
            status=ActivityStatus.QUEUED,
        )
