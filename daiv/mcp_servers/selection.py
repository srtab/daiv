from __future__ import annotations

from dataclasses import dataclass

from mcp_servers.models import MCPServer


@dataclass(frozen=True)
class PoolEntry:
    """One selectable MCP server for a run/schedule selector. ``is_default`` is True for
    ``active`` rows (pre-checked); ``on-demand`` rows render unchecked."""

    name: str
    scope: str
    description: str
    is_default: bool


def build_selection_pool(user_id: int | None = None) -> list[PoolEntry]:
    from mcp_servers.services import deduped_pool_rows

    return [
        PoolEntry(
            name=row.name,
            scope=row.scope,
            description=row.description,
            is_default=row.status == MCPServer.Status.ACTIVE,
        )
        for row in deduped_pool_rows(user_id)
    ]


def default_names(pool: list[PoolEntry]) -> set[str]:
    return {entry.name for entry in pool if entry.is_default}


def diff_selection(selected: set[str], pool: list[PoolEntry]) -> dict[str, str]:
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


def effective_selection(overrides: dict, pool: list[PoolEntry]) -> set[str]:
    """The effective checked set = default set XOR overrides, guarding ``"on"`` to pool names
    (so a demoted/deleted server's stored ``"on"`` self-heals). Inverse of ``diff_selection``."""
    names = {entry.name for entry in pool}
    selected = set(default_names(pool))
    for name, value in (overrides or {}).items():
        if value == "off":
            selected.discard(name)
        elif value == "on" and name in names:
            selected.add(name)
    return selected


def mcp_picker_context(form) -> dict:
    """Context for ``_mcp_picker.html``. Empty when the form has no ``mcp_servers`` field.
    Reads the pool the form computed in ``__init__`` (``form.mcp_pool``) and the field's
    current effective selection."""
    if "mcp_servers" not in form.fields:
        return {"mcp_pool_global": [], "mcp_pool_user": [], "mcp_selected_names": []}
    pool = getattr(form, "mcp_pool", [])
    selected = list(form["mcp_servers"].value() or [])
    return {
        "mcp_pool_global": [e for e in pool if e.scope == MCPServer.Scope.GLOBAL],
        "mcp_pool_user": [e for e in pool if e.scope == MCPServer.Scope.USER],
        "mcp_selected_names": selected,
    }
