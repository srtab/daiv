from django.db import migrations, models


def backfill_run_muted(apps, schema_editor):
    """Run.notify_on=None → muted=None (inherit); any non-null notify_on → muted=False (a prior explicit
    "never" is intentionally not preserved as silence). Schedule/template rows already default to
    muted=False via AddField."""
    Run = apps.get_model("agent_sessions", "Run")
    Run.objects.filter(notify_on__isnull=False).update(muted=False)


def backfill_ineligible(apps, schema_editor):
    """Pre-deploy terminal runs are out of scope for the reclassify backstop.

    Scoped to terminal rows so a run still RUNNING/QUEUED across the deploy stays eligible
    and is still recoverable when it finishes under the new code. Literals, not
    RunStatus.terminal(): a historical migration must freeze what "terminal" meant here.
    """
    Run = apps.get_model("agent_sessions", "Run")
    Run.objects.filter(status__in=["SUCCESSFUL", "FAILED"]).update(classify_eligible=False)


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0005_session_mcp_overrides")]

    operations = [
        migrations.AddField(
            model_name="run",
            name="muted",
            field=models.BooleanField(blank=True, default=None, null=True, verbose_name="muted"),
        ),
        migrations.RunPython(backfill_run_muted, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="run", name="run_notify_on_valid"),
        migrations.RemoveField(model_name="run", name="notify_on"),
        migrations.AddField(
            model_name="run",
            name="classify_eligible",
            field=models.BooleanField(default=True, verbose_name="classify eligible"),
        ),
        migrations.RunPython(backfill_ineligible, migrations.RunPython.noop),
    ]
