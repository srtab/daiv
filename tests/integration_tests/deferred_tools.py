from __future__ import annotations

import json

from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field

TOOL_NAME = "rt_fetch_ticket_digest"


def digest_tool() -> StructuredTool:
    """A deferred tool whose ``note_window``/``sweep_closed`` args can't be guessed from its name or from a request
    phrased as "at most 3 notes, skip resolved children", so a call with them proves the model read the schema."""

    class _Args(BaseModel):
        ticket: str = Field(description="Ticket identifier to summarize.")
        note_window: int = Field(default=5, description="Maximum number of correspondence notes to include.")
        sweep_closed: bool = Field(default=False, description="Whether to include resolved child tickets.")

    def _run(ticket: str, note_window: int = 5, sweep_closed: bool = False) -> str:
        return "digest"

    return StructuredTool.from_function(
        func=_run, name=TOOL_NAME, description="Fetch a condensed digest of an RT ticket.", args_schema=_Args
    )


def tool_search_result(tool: StructuredTool, *, embed_schema: bool) -> str:
    """The ``tool_search`` result body for loading ``tool``, with or without its embedded schema."""
    if embed_schema:
        schema = convert_to_openai_tool(tool)
        return (
            "Loaded 1 tool(s). Their full schemas follow — call them directly by name.\n\n"
            f"<functions>\n<function>{json.dumps(schema, separators=(',', ':'))}</function>\n</functions>"
        )
    return f"Loaded 1 tool(s):\n- {tool.name}: {tool.description}"
