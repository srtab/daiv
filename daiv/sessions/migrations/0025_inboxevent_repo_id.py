from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0024_session_repositories")]

    operations = [
        migrations.AddField(
            model_name="inboxevent",
            name="repo_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="The repository the event concerns when it is not the session's own; blank when it is.",
                max_length=255,
                verbose_name="repository",
            ),
        )
    ]
