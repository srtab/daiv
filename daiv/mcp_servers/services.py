from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Literal, TypedDict

from django.utils import timezone

from asgiref.sync import async_to_sync
from langchain_mcp_adapters.client import MultiServerMCPClient

from automation.agent.mcp.connections import build_connection
from automation.agent.mcp.schemas import ToolFilter, UserMcpServer
from core.encryption import DecryptionError
from mcp_servers.models import MCPServer

logger = logging.getLogger("daiv.mcp_servers")

_TEST_CONNECTION_TIMEOUT = 5.0  # seconds


class HeaderEntry(TypedDict, total=False):
    """Stored shape of a single header in ``MCPServer.headers``.

    ``mode`` is ``"literal"`` (``value`` used verbatim) or ``"env_ref"`` (``value``
    is the name of an env var resolved at runtime) — the same two strings as
    ``MCPServer.HeaderMode``, restated as a ``Literal`` because a Django
    ``TextChoices`` can't feed ``typing.Literal``. This narrows the JSON contract
    for readers; it is not enforced upstream (producers return plain ``dict`` and
    the JSON descriptor yields ``Any``), so ``_resolve_header_entries``'s runtime
    ``else`` guard remains the actual enforcement point for an unrecognized mode.
    """

    name: str
    mode: Literal["literal", "env_ref"]
    value: str


def deduped_pool_rows(user_id: int | None = None) -> list[MCPServer]:
    """Non-``disabled`` servers visible to ``user_id``, name-deduped with GLOBAL winning.

    A user-scoped row whose name matches ANY non-disabled global (active or on-demand) is
    dropped: an admin publishing a name — even on demand — controls it. Order is globals
    then user rows, each sorted by name; the returned rows carry full ``status`` for callers
    to split default vs. opt-in."""
    globals_ = list(
        MCPServer.objects
        .exclude(status=MCPServer.Status.DISABLED)
        .filter(scope=MCPServer.Scope.GLOBAL)
        .order_by("name")
    )
    user_rows: list[MCPServer] = []
    if user_id is not None:
        user_rows = list(
            MCPServer.objects
            .exclude(status=MCPServer.Status.DISABLED)
            .filter(scope=MCPServer.Scope.USER, user_id=user_id)
            .order_by("name")
        )
    global_names = {r.name for r in globals_}
    pool: list[MCPServer] = list(globals_)
    for row in user_rows:
        if row.name in global_names:
            logger.warning(
                "MCP server '%s' (pk=%s, user_id=%s) is shadowed by a non-disabled global server of the "
                "same name; excluding the user-scoped row",
                row.name,
                row.pk,
                row.user_id,
            )
            continue
        pool.append(row)
    return pool


def build_runtime_servers(user_id: int | None = None, overrides: dict | None = None) -> list[tuple[str, UserMcpServer]]:
    """Resolve the effective MCP server set for a run and convert each to a ``UserMcpServer`` DTO.

    Default set = the ``active`` rows of the deduped pool (``deduped_pool_rows``). ``overrides``
    (``{name: "on"|"off"}``) deviate from it: ``"off"`` drops a default; ``"on"`` adds a pool entry
    (a stale ``"on"`` for a disabled/deleted/unknown name self-heals to a no-op). Any value other
    than the exact strings ``"on"``/``"off"`` is ignored and logged. Empty/omitted ``overrides``
    reproduce the pure-default behavior. Header resolution and tool-filter conversion are unchanged.
    """
    overrides = overrides or {}
    pool = {row.name: row for row in deduped_pool_rows(user_id)}
    selected = {name for name, row in pool.items() if row.is_default}
    for name, value in overrides.items():
        if value == "off":
            selected.discard(name)
        elif value == "on":
            if name in pool:
                selected.add(name)
            else:
                logger.warning(
                    "MCP override '%s'='on' refers to a server absent from the pool "
                    "(disabled/deleted/shadowed); dropping from the run (user_id=%s)",
                    name,
                    user_id,
                )
        else:
            logger.warning("MCP override for '%s' has unrecognized value %r; ignoring", name, value)

    out: list[tuple[str, UserMcpServer]] = []
    for name, row in pool.items():
        if name not in selected:
            continue
        try:
            raw_headers = row.headers or []
            if row.scope == MCPServer.Scope.USER:
                raw_headers = _drop_env_refs(raw_headers, server_name=row.name)
            headers = _resolve_header_entries(raw_headers, server_name=row.name)
            tool_filter = None
            if row.tool_filter_mode != MCPServer.FilterMode.NONE and row.tool_filter_items:
                tool_filter = ToolFilter(mode=row.tool_filter_mode, items=list(row.tool_filter_items))
            dto = UserMcpServer(type=row.transport, url=row.url, headers=headers or None, tool_filter=tool_filter)
        except DecryptionError:
            logger.exception("MCP server '%s' (pk=%s) header decryption failed; skipping", row.name, row.pk)
            continue
        except Exception:  # noqa: BLE001
            # One bad row must not blank peers; skip and log.
            logger.exception(
                "MCP server '%s' (pk=%s) could not be converted to a runtime DTO; skipping", row.name, row.pk
            )
            continue
        out.append((name, dto))
    return out


def _drop_env_refs(entries: list[dict], *, server_name: str) -> list[dict]:
    """Remove ``mode="env_ref"`` headers from a user-scoped server's header list.

    ``env_ref`` resolves against the DAIV host's process environment; permitting
    it on a member-owned server would let a member exfiltrate host env vars to a
    URL they control. The form already blocks it — this is defense in depth for a
    row inserted via a raw DB write."""
    kept: list[dict] = []
    for entry in entries:
        if entry.get("mode") == MCPServer.HeaderMode.ENV_REF:
            logger.warning(
                "MCP server '%s' is user-scoped; dropping disallowed env_ref header '%s'",
                server_name,
                entry.get("name"),
            )
            continue
        kept.append(entry)
    return kept


def _resolve_header_entries(entries: list[HeaderEntry] | None, *, server_name: str) -> dict[str, str]:
    """Flatten the stored ``[{name, mode, value}]`` header shape into the
    DTO's ``dict[str, str]``.

    Literal values pass through directly; ``env_ref`` values are resolved via
    ``os.environ``. A missing env var drops that one header (logged) without
    affecting others. An unrecognized ``mode`` also drops the header with a
    warning, so a typo can't silently vanish a header.

    Shared by ``build_runtime_servers`` (runtime) and ``_build_client``
    (test-connection) so resolution and logging cannot drift between them.
    """
    resolved: dict[str, str] = {}
    for entry in entries or []:
        name = entry.get("name")
        mode = entry.get("mode")
        value = entry.get("value", "")
        if not name:
            logger.warning("MCP server '%s' has a header with no name; dropping it", server_name)
            continue
        if mode == MCPServer.HeaderMode.LITERAL:
            resolved[name] = value
        elif mode == MCPServer.HeaderMode.ENV_REF:
            env_value = os.environ.get(value)
            if env_value is None:
                logger.warning(
                    "MCP server '%s' header '%s' references missing env var '%s'; dropping header",
                    server_name,
                    name,
                    value,
                )
                continue
            resolved[name] = env_value
        else:
            logger.warning(
                "MCP server '%s' header '%s' has unrecognized mode %r; dropping header", server_name, name, mode
            )
    return resolved


def _build_client(payload: dict[str, Any]) -> MultiServerMCPClient:
    """Build a transient ``MultiServerMCPClient`` from a form-shaped payload.

    ``payload`` is ``{"transport": "http"|"sse", "url": str, "headers":
    [{"name", "mode", "value"}, ...]}``. ``mode=env_ref`` values are resolved
    against ``os.environ``; missing ones are dropped (see
    ``_resolve_header_entries``).
    """
    url = payload.get("url")
    resolved = _resolve_header_entries(payload.get("headers"), server_name=url or "test-connection")
    transport = payload.get("transport")
    connection = build_connection(transport, url, resolved or None)
    if connection is None:
        raise ValueError(f"Unsupported transport: {transport!r}")
    return MultiServerMCPClient({"__probe__": connection})


def _flatten_exception(err: BaseException) -> list[BaseException]:
    """Recursively expand ``ExceptionGroup``s into their leaf exceptions.

    The MCP streamable-http client runs its request inside an anyio task group,
    so a real failure (e.g. an httpx 401) surfaces wrapped in an
    ``ExceptionGroup`` whose ``str()`` is the useless "unhandled errors in a
    TaskGroup (N sub-exceptions)". Flattening lets ``_format_error`` report the
    underlying cause instead of the wrapper. Groups can nest, so recurse."""
    if isinstance(err, BaseExceptionGroup):
        return [leaf for sub in err.exceptions for leaf in _flatten_exception(sub)]
    return [err]


def _format_error(err: BaseException) -> str:
    """Build a human-readable one-liner for a test-connection failure, unwrapping
    any anyio ``ExceptionGroup`` to the underlying cause(s).

    ``str(err)`` is empty for many httpx/asyncio exceptions, so the class name is
    always included to keep the message greppable. Only the first non-blank line
    of each leaf is kept — httpx messages tack on a "For more information check:"
    URL that is noise in the UI. Duplicate leaves (a group can carry repeats) are
    collapsed while preserving order."""
    parts: list[str] = []
    for leaf in _flatten_exception(err):
        first_line = next((line for line in str(leaf).splitlines() if line.strip()), "")
        parts.append(f"{type(leaf).__name__}: {first_line}" if first_line else type(leaf).__name__)
    return "; ".join(dict.fromkeys(parts))


async def test_connection(payload: dict[str, Any]) -> dict[str, Any]:
    """Open a transient MCP session against ``payload`` and return either
    ``{ok: True, tools: [...]}`` or ``{ok: False, error: ...}``."""
    try:
        client = _build_client(payload)
        tools = await asyncio.wait_for(client.get_tools(), timeout=_TEST_CONNECTION_TIMEOUT)
    except TimeoutError:
        logger.warning("MCP test_connection timed out for url=%s", payload.get("url"))
        return {"ok": False, "error": f"Connection timed out after {_TEST_CONNECTION_TIMEOUT:g}s"}
    except Exception as err:  # noqa: BLE001 — surface any failure to the UI
        logger.exception("MCP test_connection failed for url=%s", payload.get("url"))
        return {"ok": False, "error": _format_error(err)}
    return {
        "ok": True,
        "tools": [
            {
                "name": t.name,
                "description": getattr(t, "description", ""),
                # MCP's optional readOnlyHint annotation, surfaced by
                # langchain-mcp-adapters on the tool's metadata. bool | None —
                # None means the server left it unannotated (unknown), not writable.
                "read_only": (getattr(t, "metadata", None) or {}).get("readOnlyHint"),
            }
            for t in tools
        ],
    }


def server_health(server: MCPServer) -> dict[str, Any]:
    """Synchronous decryption + env-ref check, no network. Flags rows that
    look loadable but whose headers ``build_runtime_servers`` would skip
    (undecryptable) or partially drop (missing env-ref var), plus literal
    headers that still carry an unexpanded ``${...}`` reference (migration
    0002 imports ``"Bearer ${TOKEN}"``-style headers verbatim because the
    model can't split the literal prefix from the ref — those never expand
    at runtime, so the badge must not report them healthy)."""
    try:
        raw = server.headers or []
    except DecryptionError:
        return {"ok": False, "reason": "headers cannot be decrypted"}
    missing = [
        entry.get("value") or "(empty)"
        for entry in raw
        if entry.get("mode") == MCPServer.HeaderMode.ENV_REF and os.environ.get(entry.get("value") or "") is None
    ]
    if missing:
        return {"ok": False, "reason": "missing env var(s): " + ", ".join(missing)}
    unexpanded = [
        entry.get("name") or "(unnamed)"
        for entry in raw
        if entry.get("mode") == MCPServer.HeaderMode.LITERAL and "${" in (entry.get("value") or "")
    ]
    if unexpanded:
        return {"ok": False, "reason": "header(s) with an unexpanded ${...} reference: " + ", ".join(unexpanded)}
    return {"ok": True, "reason": None}


def exposed_tools(server: MCPServer) -> list[dict[str, Any]]:
    """The tools ``server`` currently exposes to the agent: its persisted
    discovered catalog passed through the configured allow/block filter. Pure
    and network-free — reads only persisted fields (``discovered_tools`` plus
    the tool filter); never probes the network. Mirrors the runtime
    filter (``automation.agent.mcp.toolkits._apply_tool_filters``) via the
    shared ``ToolFilter.allows`` predicate so the two cannot drift."""
    discovered = server.discovered_tools or []
    if server.tool_filter_mode == MCPServer.FilterMode.NONE:
        return discovered
    tool_filter = ToolFilter(mode=server.tool_filter_mode, items=list(server.tool_filter_items or []))
    return [tool for tool in discovered if tool_filter.allows(tool.get("name", ""))]


def sync_discovered_tools(server: MCPServer) -> dict[str, Any]:
    """Probe ``server`` and persist its tool catalog. The single place that
    *persists* a server's discovered catalog (wrapping ``test_connection`` —
    which ``MCPServerTestView`` also calls directly for the un-persisted
    "Test connection" button).

    Wraps ``test_connection`` directly so a server that genuinely exposes zero
    tools (``ok=True, tools=[]``) is recorded as synced, while an unreachable
    server (``ok=False``) or undecryptable headers leave the previous snapshot
    and timestamp untouched — a transient failure never wipes known-good data.
    Returns ``{"ok": True, "count": n}`` on success or ``{"ok": False, "error":
    str}`` otherwise."""
    try:
        payload = {"transport": server.transport, "url": server.url, "headers": server.headers or []}
    except DecryptionError:
        logger.warning("Cannot sync tools for %r: header decryption failed.", server.name)
        return {"ok": False, "error": "headers cannot be decrypted"}
    result = async_to_sync(test_connection)(payload)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error")}
    server.discovered_tools = result["tools"]
    server.tools_synced_at = timezone.now()
    server.save(update_fields=["discovered_tools", "tools_synced_at", "modified"])
    return {"ok": True, "count": len(result["tools"])}
