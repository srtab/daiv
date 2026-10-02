from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("mcp_servers", "0004_materialize_builtin_rows")]

    operations = [
        migrations.AddField(
            model_name="mcpserver",
            name="discovered_tools",
            field=models.JSONField(blank=True, default=list, editable=False, verbose_name="discovered tools"),
        ),
        migrations.AddField(
            model_name="mcpserver",
            name="tools_synced_at",
            field=models.DateTimeField(blank=True, editable=False, null=True, verbose_name="tools synced at"),
        ),
    ]
