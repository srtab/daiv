from __future__ import annotations

import json
import logging
import os
import pathlib
import re

from django.db import migrations

from pydantic import ValidationError

logger = logging.getLogger("daiv.mcp_servers.migrations")

_ENV_REF_FULL_RE = re.compile(r"^\$\{([^}]+)\}$")


def _legacy_env(name: str) -> str | None:
    """Read a legacy ``MCP_*`` setting the way ``MCPSettings`` did before the fields were
    removed: the ``os.environ`` value takes precedence over a ``/run/secrets/<name>`` file,
    and the literal string ``"None"`` means unset (pydantic ``env_parse_none_str``). Inlined so
    this one-shot migration stays frozen and no longer imports the (now-deleted) conf fields."""
    value = os.environ.get(name)
    if value is None:
        secret = pathlib.Path("/run/secrets", name)
        if secret.exists():
            value = secret.read_text().strip()
    return None if value in (None, "None") else value


# Inlined (not imported from mcp_servers.constants) so this one-shot migration stays frozen
# even if MCP_NAME_RE changes later: lowercase alphanumerics + dashes, must start alphanumeric,
# <= 80 chars, matching the model's SlugField + RegexValidator.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")


def _normalize_name(name: str) -> str | None:
    """Coerce a legacy server key into a valid model ``name``.

    Legacy ``.mcp.json`` keys are arbitrary dict keys (``my_server``, ``MyServer``), but the
    model rejects underscores/uppercase. Importing such a key verbatim (``objects.create``
    bypasses validators) produces a row the edit form can never re-save. Returns the normalized
    name, or ``None`` if nothing valid remains (caller skips-and-warns).
    """
    candidate = _NAME_SANITIZE_RE.sub("-", name.strip().lower()).strip("-")[:80].rstrip("-")
    return candidate if candidate and _NAME_RE.match(candidate) else None


def _convert_headers(name: str, raw_headers: dict | None) -> list[dict]:
    if not raw_headers:
        return []
    out: list[dict] = []
    for header_name, value in raw_headers.items():
        if not isinstance(value, str):
            # Every other lossy path in this one-shot import warns; a non-string header value
            # (number/object in the legacy JSON) can't be stored, so drop it loudly too.
            logger.warning(
                "MCP server %r header %r has a non-string value (%s); dropping it during import.",
                name,
                header_name,
                type(value).__name__,
            )
            continue
        # Mixed strings like "Bearer ${TOKEN}" stay literal: the new model can't preserve the "Bearer " prefix.
        match = _ENV_REF_FULL_RE.match(value)
        if not match:
            if "${" in value:
                logger.warning(
                    "MCP server %r header %r mixes literal text with an env-var reference (%r); "
                    "imported as a literal — the env var will not be expanded.",
                    name,
                    header_name,
                    value,
                )
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
    from automation.agent.mcp.schemas import UserMcpServersConfig

    config_file = _legacy_env("MCP_SERVERS_CONFIG_FILE")
    if not config_file:
        return
    path = pathlib.Path(config_file)
    if not path.exists():
        return

    try:
        raw = json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        # Fail the migration/deploy loudly: this is the one-shot, non-reversible import of
        # every custom server into the DB — swallowing the error here would silently drop
        # them with no other code path left to retry the read.
        logger.exception("Failed to read MCP servers config from %s", path)
        raise

    try:
        parsed = UserMcpServersConfig.model_validate(raw)
    except ValidationError:
        logger.exception("Invalid MCP servers config in %s", path)
        return

    MCPServer = apps.get_model("mcp_servers", "MCPServer")
    from core.encryption import encrypt_value

    for raw_name, server in parsed.mcp_servers.items():
        name = _normalize_name(raw_name)
        if name is None:
            logger.warning(
                "Skipping legacy MCP server %r: its name cannot be normalized to the required format "
                "(lowercase alphanumerics + dashes). Re-create it via /dashboard/mcp-servers/.",
                raw_name,
            )
            continue
        if name != raw_name:
            logger.warning("Renaming legacy MCP server %r to %r to satisfy the naming rules.", raw_name, name)
        if MCPServer.objects.filter(name=name).exists():
            # Already imported on a prior apply, or two legacy keys normalized to the same name.
            logger.warning("Skipping legacy MCP server %r: a server named %r already exists.", raw_name, name)
            continue
        headers = _convert_headers(name, server.headers)
        # Migration uses historical model: descriptor not available — encrypt
        # the JSON blob manually using the same primitive.
        ciphertext = encrypt_value(json.dumps(headers, separators=(",", ":"))) if headers else None

        filter_mode = server.tool_filter.mode if server.tool_filter else "none"
        filter_items = server.tool_filter.items if server.tool_filter else []
        if filter_mode != "none" and not filter_items:
            # A non-"none" mode with no items would violate the constraint migration 0003
            # adds, aborting that migration. The legacy schema allows it (an empty
            # allow/block list); normalize it to "none" here instead.
            logger.warning(
                "MCP server %r had tool-filter mode %r with no items in legacy config; importing as 'none'.",
                name,
                filter_mode,
            )
            filter_mode = "none"

        MCPServer.objects.create(
            name=name,
            source="custom",
            transport=server.type,
            url=server.url,
            _headers_encrypted=ciphertext,
            tool_filter_mode=filter_mode,
            tool_filter_items=filter_items,
            enabled=True,
        )
        logger.info("Imported MCP server %r from legacy config %s as %r", raw_name, path, name)


def noop_reverse(apps, schema_editor):
    return None


class Migration(migrations.Migration):
    dependencies = [("mcp_servers", "0001_initial")]
    operations = [migrations.RunPython(import_legacy_json, noop_reverse)]
