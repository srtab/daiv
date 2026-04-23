from datetime import UTC, datetime
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest
from activity.models import Activity, ActivityStatus, TriggerType


@pytest.mark.django_db
class TestSyncStuckActivitiesCommand:
    def test_syncs_stuck_running_activity(self, create_db_task_result):
        finished = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)
        tr = create_db_task_result(
            status="SUCCESSFUL",
            return_value={"response": "Done.", "code_changes": False},
            started_at=datetime(2026, 4, 13, 11, 0, 0, tzinfo=UTC),
            finished_at=finished,
        )
        activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB, repo_id="group/project", status=ActivityStatus.RUNNING, task_result=tr
        )

        out = StringIO()
        call_command("sync_stuck_activities", stdout=out)

        activity.refresh_from_db()
        assert activity.status == ActivityStatus.SUCCESSFUL
        assert activity.finished_at == finished
        assert activity.result_summary == "Done."
        assert "Synced: 1" in out.getvalue()

    def test_skips_terminal_activities(self, create_db_task_result):
        tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "Already done."})
        Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            repo_id="group/project",
            status=ActivityStatus.SUCCESSFUL,
            task_result=tr,
            result_summary="Already done.",
        )

        out = StringIO()
        call_command("sync_stuck_activities", stdout=out)

        assert "Synced: 0" in out.getvalue()

    def test_counts_already_synced_activity_as_skipped(self, create_db_task_result):
        """A non-terminal Activity already in sync with its DBTaskResult counts toward `skipped`."""
        tr = create_db_task_result(status="READY")
        Activity.objects.create(
            trigger_type=TriggerType.API_JOB, repo_id="group/project", status=ActivityStatus.READY, task_result=tr
        )

        out = StringIO()
        call_command("sync_stuck_activities", stdout=out)

        assert "Synced: 0, already up to date: 1" in out.getvalue()

    def test_skips_activities_without_task_result(self):
        Activity.objects.create(
            trigger_type=TriggerType.ISSUE_WEBHOOK, repo_id="group/project", status=ActivityStatus.RUNNING
        )

        out = StringIO()
        call_command("sync_stuck_activities", stdout=out)

        assert "Synced: 0" in out.getvalue()

    def test_continues_after_per_row_error(self, create_db_task_result):
        ok_tr = create_db_task_result(
            status="SUCCESSFUL",
            return_value={"response": "Done."},
            finished_at=datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC),
        )
        bad_tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "boom."})

        ok_activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB, repo_id="group/project", status=ActivityStatus.RUNNING, task_result=ok_tr
        )
        bad_activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB, repo_id="group/project", status=ActivityStatus.RUNNING, task_result=bad_tr
        )

        original = Activity.sync_and_save

        def selectively_raise(self):
            if self.pk == bad_activity.pk:
                raise RuntimeError("simulated sync failure")
            return original(self)

        out = StringIO()
        with patch.object(Activity, "sync_and_save", selectively_raise), pytest.raises(CommandError) as exc_info:
            call_command("sync_stuck_activities", stdout=out)

        ok_activity.refresh_from_db()
        bad_activity.refresh_from_db()
        assert ok_activity.status == ActivityStatus.SUCCESSFUL
        assert bad_activity.status == ActivityStatus.RUNNING
        assert "Synced: 1" in str(exc_info.value)
        assert "errored: 1" in str(exc_info.value)
