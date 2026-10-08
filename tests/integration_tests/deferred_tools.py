from __future__ import annotations

import json

from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field

TOOL_NAME = "rt_fetch_ticket_digest"


def digest_tool() -> StructuredTool:
    """A deferred tool whose ``max_notes``/``include_resolved`` args can't be guessed from its name, so a call
    with them proves the model read the schema."""

    class _Args(BaseModel):
        ticket: str = Field(description="Ticket identifier to summarize.")
        max_notes: int = Field(default=5, description="Maximum number of correspondence notes to include.")
        include_resolved: bool = Field(default=False, description="Whether to include resolved child tickets.")

    def _run(ticket: str, max_notes: int = 5, include_resolved: bool = False) -> str:
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
