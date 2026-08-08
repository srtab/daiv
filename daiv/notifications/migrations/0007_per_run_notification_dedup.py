from django.db import migrations, models


def dedup_per_run_notifications(apps, schema_editor):
    """Per-run events (job.finished / schedule.finished) never had a uniqueness backstop, so rare
    duplicates may exist. Keep the earliest row per (recipient, source_type, source_id, event_type);
    the constraint add would otherwise fail."""
    Notification = apps.get_model("notifications", "Notification")
    seen: set[tuple] = set()
    dupe_ids: list = []
    qs = (
        Notification.objects
        .filter(event_type__in=["job.finished", "schedule.finished"])
        .order_by("created", "id")
        .values_list("id", "recipient_id", "source_type", "source_id", "event_type")
    )
    for pk, recipient_id, source_type, source_id, event_type in qs.iterator():
        key = (recipient_id, source_type, source_id, event_type)
        if key in seen:
            dupe_ids.append(pk)
        else:
            seen.add(key)
    if dupe_ids:
        Notification.objects.filter(id__in=dupe_ids).delete()


class Migration(migrations.Migration):
    dependencies = [("notifications", "0006_alter_notification_context")]

    operations = [
        migrations.RunPython(dedup_per_run_notifications, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="notification", name="notif_unique_per_batch_recipient"),
        migrations.AddConstraint(
            model_name="notification",
            constraint=models.UniqueConstraint(
                fields=["recipient", "source_type", "source_id", "event_type"],
                condition=models.Q(event_type__in=["job_batch.finished", "job.finished", "schedule.finished"]),
                name="notif_unique_per_source_recipient",
            ),
        ),
    ]
