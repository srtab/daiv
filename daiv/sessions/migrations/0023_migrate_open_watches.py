from django.db import migrations

OPEN_WATCH_STATES = ("watching", "fixing")


def migrate_open_watches(apps, schema_editor):
    """Each open CI watch becomes an ownership owned by its merge request's own thread, so it finishes where it is.

    ``last_daiv_sha`` stays blank: a migration cannot ask the platform, and the wake rule counts a blank one as DAIV's.
    """
    Session = apps.get_model("agent_sessions", "Session")
    MergeRequestOwnership = apps.get_model("agent_sessions", "MergeRequestOwnership")
    watches = Session.objects.filter(watch_state__in=OPEN_WATCH_STATES, merge_request_iid__isnull=False)
    MergeRequestOwnership.objects.bulk_create(
        [
            MergeRequestOwnership(
                repo_id=session.repo_id,
                merge_request_iid=session.merge_request_iid,
                session_id=session.pk,
                owner_id=session.user_id,
                source_branch=session.ref,
                attempts=session.watch_attempts,
                last_pipeline_id=session.watch_pipeline_id,
            )
            for session in watches.iterator()
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0022_merge_request_ownership")]

    operations = [migrations.RunPython(migrate_open_watches, migrations.RunPython.noop)]
