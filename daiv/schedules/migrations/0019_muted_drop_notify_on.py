from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("schedules", "0018_scheduledjob_mcp_overrides")]

    operations = [
        migrations.AddField(
            model_name="scheduledjob",
            name="muted",
            field=models.BooleanField(
                default=False, help_text="Mute notifications for this schedule.", verbose_name="muted"
            ),
        ),
        migrations.AddField(
            model_name="scheduletemplate",
            name="muted",
            field=models.BooleanField(
                default=False, help_text="Mute notifications for this schedule.", verbose_name="muted"
            ),
        ),
        migrations.RemoveField(model_name="scheduledjob", name="notify_on"),
        migrations.RemoveField(model_name="scheduletemplate", name="notify_on"),
    ]
