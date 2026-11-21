from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0043_backfill_mergerequestownership_merged")]

    operations = [
        migrations.AddField(
            model_name="mergerequestownership",
            name="draft",
            field=models.BooleanField(default=False, verbose_name="draft"),
        )
    ]
