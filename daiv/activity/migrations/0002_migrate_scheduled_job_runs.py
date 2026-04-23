"""Migrate ScheduledJobRun records to Activity and add code_changes field."""

from django.db import migrations, models


def forwards(apps, schema_editor):
    Activity = apps.get_model("activity", "Activity")
    ScheduledJobRun = apps.get_model("schedules", "ScheduledJobRun")

    activities = []
    for run in ScheduledJobRun.objects.select_related("task_result", "scheduled_job").iterator():
        status = "SUCCESSFUL"  # conservative default for pruned results
        started_at = finished_at = None
        result_summary = ""
        error_message = ""
        code_changes = False

        if run.task_result:
            status = run.task_result.status
            started_at = run.task_result.started_at
            finished_at = run.task_result.finished_at
            if run.task_result.return_value:
                result_summary = str(run.task_result.return_value)[:2000]
                if isinstance(run.task_result.return_value, dict):
                    code_changes = bool(run.task_result.return_value.get("code_changes"))
            if run.task_result.exception_class_path:
                error_message = run.task_result.exception_class_path
                if run.task_result.traceback:
                    error_message += f"\n{run.task_result.traceback}"

        activities.append(
            Activity(
                trigger_type="schedule",
                task_result=run.task_result,
                status=status,
                repo_id=run.scheduled_job.repo_id,
                ref=run.scheduled_job.ref or "",
                prompt=run.scheduled_job.prompt,
                scheduled_job=run.scheduled_job,
                result_summary=result_summary,
                error_message=error_message,
                code_changes=code_changes,
                started_at=started_at,
                finished_at=finished_at,
            )
        )

    if activities:
        Activity.objects.bulk_create(activities, batch_size=500)
        # Preserve original creation timestamps (auto_now_add sets them to migration time).
        # Join on task_result_id for records that still have a linked task result.
        schema_editor.execute(
            """
            UPDATE activity_activity
            SET created_at = r.created
            FROM schedules_scheduledjobrun r
            WHERE activity_activity.task_result_id = r.task_result_id
              AND activity_activity.task_result_id IS NOT NULL
            """
        )

    # Backfill code_changes for any Activity records already created by webhooks/API
    # (not from ScheduledJobRun) that have a linked task_result with code_changes=True.
    Activity.objects.filter(
        status="SUCCESSFUL", task_result__return_value__code_changes=True, code_changes=False
    ).update(code_changes=True)


class Migration(migrations.Migration):
    dependencies = [("activity", "0001_initial"), ("schedules", "0005_add_use_max_to_scheduledjob")]

    operations = [
        migrations.AddField(
            model_name="activity",
            name="code_changes",
            field=models.BooleanField(default=False, verbose_name="code changes"),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
