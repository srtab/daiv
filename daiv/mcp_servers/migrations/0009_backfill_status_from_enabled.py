from django.db import migrations

from mcp_servers.migrations._status_backfill_ops import backfill, unbackfill


def _forward(apps, schema_editor):
    backfill(apps.get_model("mcp_servers", "MCPServer"))


def _reverse(apps, schema_editor):
    unbackfill(apps.get_model("mcp_servers", "MCPServer"))


class Migration(migrations.Migration):
    dependencies = [("mcp_servers", "0008_mcpserver_status")]
    operations = [migrations.RunPython(_forward, _reverse)]
