from django.db import migrations, models


def backfill_ineligible(apps, schema_editor):
    """Pre-deploy terminal runs are out of scope for the reclassify backstop.

    Scoped to terminal rows so a run still RUNNING/QUEUED across the deploy stays eligible
    and is still recoverable when it finishes under the new code. Literals, not
    RunStatus.terminal(): a historical migration must freeze what "terminal" meant here.
    """
    Run = apps.get_model("agent_sessions", "Run")
    Run.objects.filter(status__in=["SUCCESSFUL", "FAILED"]).update(classify_eligible=False)


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0008_merge_0005_session_mcp_overrides_0007_drop_notify_on")]

    operations = [
        migrations.AddField(
            model_name="run",
            name="classify_eligible",
            field=models.BooleanField(default=True, verbose_name="classify eligible"),
        ),
        migrations.RunPython(backfill_ineligible, migrations.RunPython.noop),
    ]
