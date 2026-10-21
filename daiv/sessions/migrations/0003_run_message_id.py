from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="run",
            name="message_id",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="message ID"),
        )
    ]
