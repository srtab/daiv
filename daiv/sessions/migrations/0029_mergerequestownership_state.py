from django.db import migrations, models


def backfill_state(apps, schema_editor):
    """Existing closed ownerships predate the ``state`` column, so set them to ``closed``.

    ``merged`` vs ``closed`` can't be recovered retroactively; ``closed`` is the safer display than
    leaving a merged/closed MR reading as ``open``.
    """
    MergeRequestOwnership = apps.get_model("agent_sessions", "MergeRequestOwnership")
    MergeRequestOwnership.objects.filter(closed_at__isnull=False).update(state="closed")


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0028_run_merge_request_title_repo_publishes")]

    operations = [
        migrations.AddField(
            model_name="mergerequestownership",
            name="state",
            field=models.CharField(
                choices=[("open", "Open"), ("merged", "Merged"), ("closed", "Closed")],
                default="open",
                max_length=10,
                verbose_name="merge request state",
            ),
        ),
        migrations.RunPython(backfill_state, migrations.RunPython.noop),
    ]
