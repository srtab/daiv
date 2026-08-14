from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0004_runenvelope")]

    operations = [
        migrations.AddField(
            model_name="session",
            name="mcp_servers",
            field=models.JSONField(blank=True, default=None, null=True, verbose_name="MCP servers"),
        )
    ]
