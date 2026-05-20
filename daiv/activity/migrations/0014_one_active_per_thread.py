from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("activity", "0013_queued_status_and_thread_status_index")]

    operations = [
        migrations.AddConstraint(
            model_name="activity",
            constraint=models.UniqueConstraint(
                fields=["thread_id"],
                condition=models.Q(status__in=["READY", "RUNNING"], trigger_type__in=["api_job", "mcp_job"]),
                name="activity_one_active_per_thread",
            ),
        )
    ]
