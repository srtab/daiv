from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0028_run_merge_request_title_repo_publishes")]

    operations = [
        migrations.AddField(
            model_name="mergerequestownership",
            name="merged",
            field=models.BooleanField(default=False, verbose_name="merged"),
        )
    ]
