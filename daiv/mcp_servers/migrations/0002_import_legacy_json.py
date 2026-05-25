from __future__ import annotations

import json
import logging
import pathlib
import re

from django.db import migrations

logger = logging.getLogger("daiv.mcp_servers.migrations")

_ENV_REF_RE = re.compile(r"\$\{([^}]+)\}")


def _convert_headers(name: str, raw_headers: dict | None) -> list[dict]:
    if not raw_headers:
        return []
    out: list[dict] = []
    for header_name, value in raw_headers.items():
        match = _ENV_REF_RE.search(value)
        if not match:
            out.append({"name": header_name, "mode": "literal", "value": value})
            continue
        expr = match.group(1)
        if ":-" in expr:
            var_name, default = expr.split(":-", 1)
            logger.warning(
                "MCP server %r header %r used '${%s:-%s}' default syntax during import; "
                "the fallback default value %r is dropped (not supported in the new model).",
                name,
                header_name,
                var_name,
                default,
                default,
            )
            out.append({"name": header_name, "mode": "env_ref", "value": var_name})
        else:
            out.append({"name": header_name, "mode": "env_ref", "value": expr})
    return out


def import_legacy_json(apps, schema_editor):
    from automation.agent.mcp.conf import settings as mcp_conf
    from automation.agent.mcp.schemas import UserMcpServersConfig

    if not mcp_conf.SERVERS_CONFIG_FILE:
        return
    path = pathlib.Path(mcp_conf.SERVERS_CONFIG_FILE)
    if not path.exists():
        return

    try:
        raw = json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        logger.exception("Failed to read MCP servers config from %s", path)
        return

    try:
        parsed = UserMcpServersConfig.model_validate(raw)
    except Exception:
        logger.exception("Invalid MCP servers config in %s", path)
        return

    MCPServer = apps.get_model("mcp_servers", "MCPServer")
    from core.encryption import encrypt_value

    for name, server in parsed.mcp_servers.items():
        if MCPServer.objects.filter(name=name).exists():
            continue
        headers = _convert_headers(name, server.headers)
        # Migration uses historical model: descriptor not available — encrypt
        # the JSON blob manually using the same primitive.
        ciphertext = encrypt_value(json.dumps(headers, separators=(",", ":"))) if headers else None
        MCPServer.objects.create(
            name=name,
            source="custom",
            transport=server.type,
            url=server.url,
            _headers_encrypted=ciphertext,
            tool_filter_mode=server.tool_filter.mode if server.tool_filter else "none",
            tool_filter_items=server.tool_filter.items if server.tool_filter else [],
            enabled=True,
        )
        logger.info("Imported MCP server %r from legacy config %s", name, path)


def noop_reverse(apps, schema_editor):
    return None


class Migration(migrations.Migration):
    dependencies = [("mcp_servers", "0001_initial")]
    operations = [migrations.RunPython(import_legacy_json, noop_reverse)]
