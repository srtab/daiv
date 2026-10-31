from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone

INBOX_TRIGGERS = ["chat", "mr_webhook", "api_job", "mcp_job", "delegation_resume", "pipeline_webhook"]
SUPERSEDED_MESSAGE = (
    "Not started: the session inbox replaced the queue this run waited in, and another run was already queued on "
    "this branch. Submit it again."
)


def fail_extra_queued_runs(apps, schema_editor):
    """Fail every QUEUED run past the oldest on a session's branch, which the old FIFO queue could stack and the new
    constraint forbids."""
    Run = apps.get_model("agent_sessions", "Run")
    queued = Run.objects.filter(status="QUEUED", trigger_type__in=INBOX_TRIGGERS)
    crowded = queued.values("session_id", "ref").annotate(n=Count("id")).filter(n__gt=1)
    for group in crowded:
        extra = queued.filter(session_id=group["session_id"], ref=group["ref"]).order_by("created_at", "id")[1:]
        Run.objects.filter(pk__in=[run.pk for run in extra]).update(
            status="FAILED", finished_at=timezone.now(), error_message=SUPERSEDED_MESSAGE
        )


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0023_migrate_open_watches")]

    operations = [
        migrations.RunPython(fail_extra_queued_runs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="run",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "QUEUED"), ("trigger_type__in", INBOX_TRIGGERS)),
                fields=("session", "ref"),
                name="run_one_next_run_per_session",
            ),
        ),
    ]
