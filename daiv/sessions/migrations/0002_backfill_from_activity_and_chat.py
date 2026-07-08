from django.db import migrations

from sessions.backfill import run_backfill


class Migration(migrations.Migration):
    dependencies = [
        ("agent_sessions", "0001_initial"),
        # Pinned intentionally to the last source-app migration BEFORE the tables are
        # dropped. This backfill must read the pre-drop Activity/ChatThread columns, so
        # it has to run before ``activity 0016`` / ``chat 0004`` (the DeleteModel ops,
        # which depend on this migration). Do NOT advance these pins to a later
        # migration — that would break the backfill.
        ("activity", "0015_activity_agent_override_fields"),
        ("chat", "0003_chatthread_agent_override_fields"),
    ]

    operations = [migrations.RunPython(run_backfill, migrations.RunPython.noop)]
