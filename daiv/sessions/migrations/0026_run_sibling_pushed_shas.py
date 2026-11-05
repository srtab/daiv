from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0025_inboxevent_repo_id")]

    operations = [
        migrations.AddField(
            model_name="run",
            name="sibling_pushed_shas",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "The commits the run pushed to its session's other repositories, without which repository each "
                    "went to."
                ),
                verbose_name="sibling pushed commits",
            ),
        )
    ]
