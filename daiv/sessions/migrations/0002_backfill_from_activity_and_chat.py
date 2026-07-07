from django.db import migrations

from sessions.backfill import run_backfill


class Migration(migrations.Migration):
    dependencies = [
        ("agent_sessions", "0001_initial"),
        # Latest migration of each source app as of plan-writing; if new migrations
        # landed since, re-check with: ls daiv/activity/migrations/ | tail -1
        ("activity", "0015_activity_agent_override_fields"),
        ("chat", "0003_chatthread_agent_override_fields"),
    ]

    operations = [migrations.RunPython(run_backfill, migrations.RunPython.noop)]
