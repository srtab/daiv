from django.db import migrations, models


def backfill_merged(apps, schema_editor):
    """Mark merged the ownerships 0029 left ``merged=False`` though a merge closed them, as the ``mr_merged`` event the
    close posted records, in whichever session it went to. An event's blank ``repo_id`` means its session's repository."""
    InboxEvent = apps.get_model("agent_sessions", "InboxEvent")
    MergeRequestOwnership = apps.get_model("agent_sessions", "MergeRequestOwnership")
    merge_event = InboxEvent.objects.filter(
        models.Q(repo_id=models.OuterRef("repo_id"))
        | models.Q(repo_id="", session__repo_id=models.OuterRef("repo_id")),
        merge_request_iid=models.OuterRef("merge_request_iid"),
        kind="mr_merged",
    )
    MergeRequestOwnership.objects.filter(closed_at__isnull=False, merged=False).filter(
        models.Exists(merge_event)
    ).update(merged=True)


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0042_spendrecord_counts_run_deleted_user")]

    operations = [migrations.RunPython(backfill_merged, migrations.RunPython.noop)]
