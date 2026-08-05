from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Annotated

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command
from pydantic import BeforeValidator

from automation.agent.deferred.conf import settings as deferred_settings
from automation.agent.deferred.state import DeferredToolsState  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.tools import BaseTool

    from automation.agent.deferred.index import DeferredToolsIndex, ToolEntry

logger = logging.getLogger("daiv.tools")

TOOL_SEARCH_NAME = "tool_search"


def _coerce_select(value: object) -> object:
    # Some models (observed: Qwen 3.x) JSON-stringify array parameters, sending
    # `'["gitlab"]'` instead of `["gitlab"]`. Pydantic rejects the string and the
    # model burns 4+ retries before recovering. Parse a JSON-encoded list back to
    # a list; treat any other string as a single-name shorthand.
    #
    # The shorthand path emits a debug log so operators can spot a model that
    # routinely mis-serializes — the downstream "None of the requested names
    # are deferred tools" branch is the user-facing signal, but it doesn't
    # distinguish "asked for a name that doesn't exist" from "model sent garbage".
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        logger.debug("tool_search: coercing non-JSON `select=%r` to single-name list", value)
        return [value]
    if isinstance(parsed, list):
        return parsed
    logger.debug("tool_search: coercing non-list JSON `select=%r` (parsed=%r) to single-name list", value, parsed)
    return [value]


TOOL_SEARCH_DESCRIPTION = """\
Load deferred tools by exact name or, as a fallback, by keyword search.

Tool names listed in <available-deferred-tools> are deferred — their schemas
are not loaded by default. Loaded tools remain available for the rest of this
session.

Prefer `select` (a JSON array of exact names from <available-deferred-tools>)
— that is faster and more precise than a query. Use `query` (a keyword string)
only when you cannot identify the right tool from the list.

`select` is an array, never a string. Pass `["gitlab"]`, not `"[\"gitlab\"]"`
or `"gitlab"` — a string value will be rejected with a validation error.

Examples (parameter values shown in JSON form):
  - select: ["gitlab"]                                        # preferred when the name is known
  - select: ["sentry_find_organizations", "sentry_list_issues"]
  - query: "open pull request"                                # only when browsing by capability"""


def _render_loaded(entries: list[ToolEntry]) -> str:
    """Render the tool_search result body.

    With schema embedding on (default), each loaded tool's full OpenAI-format schema rides in a
    ``<functions>`` block so an allowlisted model can call it directly without the tool being in
    its bound array. Re-selecting an already-loaded name re-sends its schema, so a
    summarization-evicted schema stays recoverable. With the valve off, falls back to name/summary
    lines only (pre-change behaviour).
    """
    if not deferred_settings.EMBED_SCHEMAS_IN_RESULTS:
        body = "\n".join(f"- {entry.name}: {entry.summary}" for entry in entries)
        return f"Loaded {len(entries)} tool(s):\n{body}"

    functions: list[str] = []
    for entry in entries:
        try:
            schema = convert_to_openai_tool(entry.tool)
        except Exception:
            logger.debug("tool_search: no JSON schema for %s; embedding summary only", entry.name)
            functions.append(f"<function-summary>{entry.name}: {entry.summary}</function-summary>")
            continue
        functions.append(f"<function>{json.dumps(schema, separators=(',', ':'))}</function>")
    body = "\n".join(functions)
    header = f"Loaded {len(entries)} tool(s). Their full schemas follow — call them directly by name."
    return f"{header}\n\n<functions>\n{body}\n</functions>"


def make_tool_search(get_index: Callable[[], DeferredToolsIndex], *, top_k_default: int, top_k_max: int) -> BaseTool:
    async def tool_search(
        runtime: ToolRuntime[object, DeferredToolsState],
        select: Annotated[
            list[str] | None,
            BeforeValidator(_coerce_select),
            'JSON array of exact tool names — e.g. ["gitlab"]. Preferred when names are known.',
        ] = None,
        query: Annotated[str, "Keywords describing the capability. Use only when `select` cannot be used."] = "",
        top_k: Annotated[int | None, "Number of search results to return."] = None,
    ) -> Command:
        index = get_index()

        missing: list[str] = []
        if select:
            entries = []
            for name in select:
                entry = index.get(name)
                if entry is None:
                    missing.append(name)
                else:
                    entries.append(entry)
        else:
            effective_top_k = min(top_k or top_k_default, top_k_max)
            entries = index.search(query, top_k=effective_top_k)

        if not entries:
            if select and missing:
                content = f"None of the requested names are deferred tools: {', '.join(missing)}."
            elif select:
                content = "Empty `select` list — pass at least one tool name."
            else:
                content = f"No deferred tools matched query {query!r}."
            return Command(update={"messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)]})

        existing = runtime.state.get("loaded_tool_names") or set()
        new_loaded = existing | {entry.name for entry in entries}

        content = _render_loaded(entries)
        if missing:
            content += f"\n\nIgnored unknown names: {', '.join(missing)}"
        return Command(
            update={
                "loaded_tool_names": new_loaded,
                "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
            }
        )

    return tool(TOOL_SEARCH_NAME, description=TOOL_SEARCH_DESCRIPTION)(tool_search)
