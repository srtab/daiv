from django.db import migrations


def backfill_run_muted(apps, schema_editor):
    """Run.notify_on=None → muted=None (inherit); any non-null notify_on → muted=False (a prior explicit
    "never" is intentionally not preserved as silence). Schedule/template rows already default to
    muted=False via AddField."""
    Run = apps.get_model("agent_sessions", "Run")
    Run.objects.filter(notify_on__isnull=False).update(muted=False)


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0005_add_muted")]

    operations = [migrations.RunPython(backfill_run_muted, migrations.RunPython.noop)]
