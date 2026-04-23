from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from django_tasks.signals import task_finished, task_started

from accounts.models import User


def _create_activity(*, task_result=None, status=ActivityStatus.READY, **kwargs):
    defaults = {
        "trigger_type": TriggerType.API_JOB,
        "repo_id": "group/project",
        "status": status,
        "task_result": task_result,
    }
    defaults.update(kwargs)
    return Activity.objects.create(**defaults)


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


@pytest.mark.django_db
class TestSyncActivityOnTaskSignals:
    def test_task_finished_syncs_successful_activity(self, create_db_task_result):
        finished = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)
        tr = create_db_task_result(
            status="SUCCESSFUL",
            return_value={"response": "Job done.", "code_changes": True, "merge_request_id": 42},
            started_at=datetime(2026, 4, 13, 11, 0, 0, tzinfo=UTC),
            finished_at=finished,
        )
        activity = _create_activity(task_result=tr, status=ActivityStatus.READY)

        task_finished.send(sender=type(None), task_result=tr.task_result)

        activity.refresh_from_db()
        assert activity.status == ActivityStatus.SUCCESSFUL
        assert activity.finished_at == finished
        assert activity.result_summary == "Job done."
        assert activity.code_changes is True
        assert activity.merge_request_iid == 42

    def test_task_finished_syncs_failed_activity(self, create_db_task_result):
        tr = create_db_task_result(
            status="FAILED",
            exception_class_path="builtins.ValueError",
            traceback="Traceback (most recent call last): ...",
            started_at=datetime(2026, 4, 13, 11, 0, 0, tzinfo=UTC),
            finished_at=datetime(2026, 4, 13, 11, 5, 0, tzinfo=UTC),
        )
        activity = _create_activity(task_result=tr, status=ActivityStatus.RUNNING)

        task_finished.send(sender=type(None), task_result=tr.task_result)

        activity.refresh_from_db()
        assert activity.status == ActivityStatus.FAILED
        assert "ValueError" in activity.error_message
        assert "Traceback" in activity.error_message

    def test_task_started_syncs_running_status(self, create_db_task_result):
        started = datetime(2026, 4, 13, 11, 0, 0, tzinfo=UTC)
        tr = create_db_task_result(status="RUNNING", started_at=started)
        activity = _create_activity(task_result=tr, status=ActivityStatus.READY)

        task_started.send(sender=type(None), task_result=tr.task_result)

        activity.refresh_from_db()
        assert activity.status == ActivityStatus.RUNNING
        assert activity.started_at == started

    def test_task_finished_no_activity_does_not_raise(self, create_db_task_result):
        tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "done"})

        task_finished.send(sender=type(None), task_result=tr.task_result)

    def test_task_started_no_activity_does_not_raise(self, create_db_task_result):
        tr = create_db_task_result(status="RUNNING")

        task_started.send(sender=type(None), task_result=tr.task_result)

    def test_signal_handler_swallows_sync_errors(self, create_db_task_result):
        tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "done"})
        _create_activity(task_result=tr, status=ActivityStatus.READY)

        with patch.object(Activity, "sync_from_task_result", side_effect=RuntimeError("boom")):
            task_finished.send(sender=type(None), task_result=tr.task_result)


@pytest.mark.django_db
class TestActivityFinishedSignal:
    def test_emitted_on_transition_to_successful(self, member_user):
        from unittest.mock import MagicMock

        from activity.signals import activity_finished, emit_activity_finished_if_terminal

        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE, user=member_user, repo_id="r/x", status=ActivityStatus.RUNNING
        )
        received = MagicMock()
        activity_finished.connect(received, dispatch_uid="test-succ")
        try:
            activity.status = ActivityStatus.SUCCESSFUL
            activity.save()
            emit_activity_finished_if_terminal(activity, previous_status=ActivityStatus.RUNNING)

            assert received.called
            _, kwargs = received.call_args
            assert kwargs["activity"] is activity
        finally:
            activity_finished.disconnect(dispatch_uid="test-succ")

    def test_not_emitted_when_still_running(self, member_user):
        from unittest.mock import MagicMock

        from activity.signals import activity_finished, emit_activity_finished_if_terminal

        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE, user=member_user, repo_id="r/x", status=ActivityStatus.RUNNING
        )
        received = MagicMock()
        activity_finished.connect(received, dispatch_uid="test-run")
        try:
            emit_activity_finished_if_terminal(activity, previous_status=ActivityStatus.READY)
            assert not received.called
        finally:
            activity_finished.disconnect(dispatch_uid="test-run")

    def test_not_emitted_when_already_terminal(self, member_user):
        from unittest.mock import MagicMock

        from activity.signals import activity_finished, emit_activity_finished_if_terminal

        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE, user=member_user, repo_id="r/x", status=ActivityStatus.SUCCESSFUL
        )
        received = MagicMock()
        activity_finished.connect(received, dispatch_uid="test-term")
        try:
            emit_activity_finished_if_terminal(activity, previous_status=ActivityStatus.SUCCESSFUL)
            assert not received.called
        finally:
            activity_finished.disconnect(dispatch_uid="test-term")
