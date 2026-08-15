from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mcp_servers.models import MCPServer

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("daiv.mcp_servers")


@dataclass(frozen=True)
class PoolEntry:
    """One selectable MCP server for a run/schedule selector. ``is_default`` is True for
    ``active`` rows (pre-checked); ``on-demand`` rows render unchecked."""

    name: str
    scope: Literal["global", "user"]
    description: str
    is_default: bool


def pool_from_rows(rows: Sequence[MCPServer]) -> list[PoolEntry]:
    """Project resolved pool rows onto the selector's view of them. Callers that already hold
    the rows (the composer context, the runtime path) go through this instead of re-querying,
    so one read feeds both the catalog they render and the selection they resolve."""
    return [
        PoolEntry(name=row.name, scope=row.scope, description=row.description, is_default=row.is_default)
        for row in rows
    ]


def build_selection_pool(user_id: int | None = None) -> list[PoolEntry]:
    from mcp_servers.services import deduped_pool_rows

    return pool_from_rows(deduped_pool_rows(user_id))


MAX_SERVER_NAMES = 200
MAX_SERVER_NAME_LENGTH = MCPServer._meta.get_field("name").max_length


def parse_server_names(raw: object) -> list[str]:
    """Validate a raw MCP selection payload into a stripped, deduped list of server names.
    Raises ``ValueError`` otherwise so each caller can translate it to its own error type
    (``HttpError`` for the chat endpoint, ``ValidationError`` for the run/schedule form).

    Both bounds matter: the count alone leaves each entry unbounded, and every caller here
    parses client-supplied JSON straight into a column ``diff_selection`` will not shrink
    (an unknown name is dropped from the diff, but only after it has been held in memory).
    """
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ValueError("mcp_servers must be a list of server names.")
    if len(raw) > MAX_SERVER_NAMES:
        raise ValueError(f"mcp_servers must name at most {MAX_SERVER_NAMES} servers.")
    names = [name.strip() for name in raw]
    if any(not name or len(name) > MAX_SERVER_NAME_LENGTH for name in names):
        raise ValueError(
            f"mcp_servers entries must be non-empty server names of at most {MAX_SERVER_NAME_LENGTH} characters."
        )
    return list(dict.fromkeys(names))


def default_names(pool: Sequence[PoolEntry]) -> set[str]:
    return {entry.name for entry in pool if entry.is_default}


def diff_selection(selected: set[str], pool: Sequence[PoolEntry]) -> dict[str, str]:
    """Server-side diff: emit only deviations from the live default set. A name not in ``pool``
    is ignored, so a stale checked box can never store an override for a vanished server."""
    overrides: dict[str, str] = {}
    for entry in pool:
        checked = entry.name in selected
        if checked and not entry.is_default:
            overrides[entry.name] = "on"
        elif entry.is_default and not checked:
            overrides[entry.name] = "off"
    return overrides


def effective_selection(overrides: dict, pool: Sequence[PoolEntry], *, warn: bool = False) -> set[str]:
    """The effective checked set: the default set with ``diff_selection``'s deviations applied
    (``"off"`` drops a default, ``"on"`` adds a pool entry), guarding ``"on"`` to pool names
    (so a disabled/deleted server's stored ``"on"`` self-heals). Inverse of ``diff_selection``.

    ``warn`` logs the two override values that resolve to nothing — a stale ``"on"`` and an
    unrecognized value. Only the runtime path sets it: the display paths re-resolve on every
    page render, where the same warning says nothing new once per navigation."""
    names = {entry.name for entry in pool}
    selected = default_names(pool)
    for name, value in (overrides or {}).items():
        if value == "off":
            selected.discard(name)
        elif value == "on":
            if name in names:
                selected.add(name)
            elif warn:
                logger.warning(
                    "MCP override '%s'='on' refers to a server absent from the pool "
                    "(disabled/deleted/shadowed); dropping it from the selection",
                    name,
                )
        elif warn:
            logger.warning("MCP override for '%s' has unrecognized value %r; ignoring", name, value)
    return selected


def composer_mcp_context(user, overrides: dict | None = None) -> dict:
    """Context for the chat composer's Tools group: one row per MCP server, plus the names
    ``overrides`` currently resolve to. Both read the *viewer's* pool in a single query, which
    is the pool ``create_chat_completion`` diffs their next turn against — resolving it twice
    would let an admin's status flip land between the two and hand the sheet a catalog and a
    selection computed against different pools."""
    from mcp_servers.services import composer_server_rows, deduped_pool_rows

    rows = deduped_pool_rows(getattr(user, "pk", None))
    return {
        "mcp_server_rows": composer_server_rows(rows),
        "mcp_servers_selected": sorted(effective_selection(overrides or {}, pool_from_rows(rows))),
    }


def mcp_picker_context(form) -> dict:
    """Context for ``_mcp_picker.html``. Empty when the form has no ``mcp_servers`` field.
    Reads the pool the form computed in ``__init__`` (``form.mcp_pool``) and the field's
    current effective selection."""
    has_field = "mcp_servers" in form.fields
    pool = getattr(form, "mcp_pool", []) if has_field else []
    selected = list(form["mcp_servers"].value() or []) if has_field else []
    return {
        "mcp_pool_global": [e for e in pool if e.scope == MCPServer.Scope.GLOBAL],
        "mcp_pool_user": [e for e in pool if e.scope == MCPServer.Scope.USER],
        # Autoescaping turns the quotes into `&quot;` inside the attribute, which the
        # browser hands back as valid JSON.
        "mcp_selected_json": json.dumps(selected),
    }
