"""Live gate for adding a model to DEFERRED_TOOLS_FROZEN_TOOLS_MODELS.

Excluded from ``make test`` (lives under tests/integration_tests). Requires real provider keys;
each parametrization is skipped when its key is absent. Three modes per model:

  * Mode 1 (fallback): schema in the tool_search result AND the tool bound. Must PASS for every
    model — this is the non-allowlisted production shape.
  * Mode 2 (frozen): schema in the result, tool NOT bound. Must PASS to allowlist the model.
  * Mode 3 (control): summary only, no schema, tool not bound. Must NOT yield correct typed args —
    proves a mode-1/2 pass really measures schema-reading.

``max_notes``/``include_resolved`` are unguessable from the tool name, so correct args prove the
schema was read rather than the name pattern-matched.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field

from automation.agent import BaseAgent
from automation.agent.deferred.index import DeferredToolsIndex
from automation.agent.deferred.prompt import build_deferred_tools_block
from automation.agent.deferred.search_tool import TOOL_SEARCH_NAME, make_tool_search

from .utils import require_provider_for_model

TOOL_NAME = "rt_fetch_ticket_digest"


def _digest_tool() -> StructuredTool:
    class _Args(BaseModel):
        ticket: str = Field(description="Ticket identifier to summarize.")
        max_notes: int = Field(default=5, description="Maximum number of correspondence notes to include.")
        include_resolved: bool = Field(default=False, description="Whether to include resolved child tickets.")

    def _run(ticket: str, max_notes: int = 5, include_resolved: bool = False) -> str:
        return "digest"

    return StructuredTool.from_function(
        func=_run, name=TOOL_NAME, description="Fetch a condensed digest of an RT ticket.", args_schema=_Args
    )


def _conversation(*, embed_schema: bool) -> list:
    tool = _digest_tool()
    index = DeferredToolsIndex([tool])
    if embed_schema:
        schema = convert_to_openai_tool(tool)
        result = (
            "Loaded 1 tool(s). Their full schemas follow — call them directly by name.\n\n"
            f"<functions>\n<function>{json.dumps(schema, separators=(',', ':'))}</function>\n</functions>"
        )
    else:
        result = f"Loaded 1 tool(s):\n- {TOOL_NAME}: Fetch a condensed digest of an RT ticket."

    return [
        SystemMessage(content=build_deferred_tools_block(index)),
        HumanMessage(content="Fetch a digest of ticket ABC-123, at most 3 notes, and skip resolved children."),
        AIMessage(
            content="",
            tool_calls=[
                {"name": TOOL_SEARCH_NAME, "id": "call_ts", "args": {"select": [TOOL_NAME]}, "type": "tool_call"}
            ],
        ),
        ToolMessage(content=result, tool_call_id="call_ts"),
    ]


def _bound_model(model_spec: str, *, bind_digest: bool):
    model = BaseAgent.get_model(model=model_spec)
    tools = [make_tool_search(lambda: DeferredToolsIndex([_digest_tool()]), top_k_default=5, top_k_max=10)]
    if bind_digest:
        tools.append(_digest_tool())
    return model.bind_tools(tools)


def _called_digest_with_typed_args(response: AIMessage) -> bool:
    for call in response.tool_calls or []:
        if call.get("name") != TOOL_NAME:
            continue
        args = call.get("args") or {}
        # A real schema read yields the unguessable param names; string coercions ("3"/"true") count.
        return "max_notes" in args or "include_resolved" in args
    return False


# (model_spec, is_allowlist_candidate). Candidates must also pass Mode 2; non-candidates need only
# Mode 1. See the spec's verification matrix.
_MODELS = [
    pytest.param("openrouter:anthropic/claude-sonnet-4.6", True, id="openrouter-claude-sonnet"),
    pytest.param("openrouter:z-ai/glm-5.1", False, id="openrouter-glm"),
    pytest.param("openrouter:openai/gpt-5.3-codex", False, id="openrouter-gpt-codex"),
]


@pytest.mark.deferred_frozen
@pytest.mark.parametrize("model_spec,is_candidate", _MODELS)
async def test_mode1_fallback_reaches_tool(model_spec, is_candidate):
    require_provider_for_model(model_spec)
    model = _bound_model(model_spec, bind_digest=True)
    response = await model.ainvoke(_conversation(embed_schema=True))
    assert _called_digest_with_typed_args(response), f"Mode 1 must pass for every model; {model_spec} did not"


@pytest.mark.deferred_frozen
@pytest.mark.parametrize("model_spec,is_candidate", _MODELS)
async def test_mode2_frozen_reaches_tool(model_spec, is_candidate):
    require_provider_for_model(model_spec)
    model = _bound_model(model_spec, bind_digest=False)
    response = await model.ainvoke(_conversation(embed_schema=True))
    passed = _called_digest_with_typed_args(response)
    if is_candidate:
        assert passed, f"Allowlist candidate {model_spec} failed Mode 2 (frozen array) — do not allowlist it"
    elif not passed:
        pytest.xfail(f"{model_spec} is a known Mode-2 non-passer (livelocks on the frozen array)")


@pytest.mark.deferred_frozen
@pytest.mark.parametrize("model_spec,is_candidate", _MODELS)
async def test_mode3_control_does_not_reach_tool(model_spec, is_candidate):
    require_provider_for_model(model_spec)
    model = _bound_model(model_spec, bind_digest=False)
    response = await model.ainvoke(_conversation(embed_schema=False))
    assert not _called_digest_with_typed_args(response), (
        f"Control violated: {model_spec} produced correct args with no schema present"
    )
