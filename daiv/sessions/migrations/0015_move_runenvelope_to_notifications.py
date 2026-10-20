from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0014_sessionarchive")]

    operations = [
        # notifications.0012_runenvelope adopts the table as is; nothing is dropped.
        migrations.SeparateDatabaseAndState(state_operations=[migrations.DeleteModel(name="RunEnvelope")])
    ]
