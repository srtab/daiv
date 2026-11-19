from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0038_backfill_spendrecord")]

    operations = [
        migrations.AddField(
            model_name="spendrecord",
            name="cost_reported",
            field=models.BooleanField(default=False, verbose_name="cost reported"),
        )
    ]
