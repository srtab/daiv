import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("memory", "0001_initial"), ("agent_sessions", "0001_initial")]
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
        )
    ]
