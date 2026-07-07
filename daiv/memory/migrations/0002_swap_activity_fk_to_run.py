import django.db.models.deletion
from django.db import migrations, models


def copy_activity_fk(apps, schema_editor):
    MemoryObservation = apps.get_model("memory", "MemoryObservation")
    Run = apps.get_model("agent_sessions", "Run")
    run_ids = set(Run.objects.values_list("id", flat=True))
    for obs in MemoryObservation.objects.exclude(activity__isnull=True).iterator():
        if obs.activity_id in run_ids:
            obs.run_id = obs.activity_id
            obs.save(update_fields=["run_id"])


class Migration(migrations.Migration):
    dependencies = [("memory", "0001_initial"), ("agent_sessions", "0002_backfill_from_activity_and_chat")]
    operations = [
        migrations.AddField(
            model_name="memoryobservation",
            name="run",
            field=models.ForeignKey(
                to="agent_sessions.run",
                on_delete=django.db.models.deletion.SET_NULL,
                null=True,
                blank=True,
                related_name="memory_observations",
                verbose_name="run",
            ),
        ),
        migrations.RunPython(copy_activity_fk, migrations.RunPython.noop),
        migrations.RemoveField(model_name="memoryobservation", name="activity"),
    ]
