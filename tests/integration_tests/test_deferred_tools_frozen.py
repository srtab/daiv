"""Live gate for adding a model to DEFERRED_TOOLS_FROZEN_TOOLS_MODELS.

Excluded from ``make test`` (lives under tests/integration_tests). Requires real provider keys;
each parametrization is skipped when its key is absent. Three modes per model:

  * Mode 1 (fallback): schema in the tool_search result AND the tool bound. Must PASS for every
    model — this is the non-allowlisted production shape.
  * Mode 2 (frozen): schema in the result, tool NOT bound. Must PASS to allowlist the model.
  * Mode 3 (control): summary only, no schema, tool not bound. Must NOT yield correct typed args —
    proves a mode-1/2 pass really measures schema-reading.

``note_window``/``sweep_closed`` can't be inferred from the tool name or the request's wording, so correct args
prove the schema was read. Every failure lists what each attempt actually did.

Each mode runs ``DAIV_EVAL_REPEATS`` times (default 3) and every attempt must agree: a frozen model that
livelocks once in three is a stuck run in production. Models come in three groups (see ``utils.py``):

  * allowlisted: already frozen; Mode 2 guards against a regression.
  * rejected: gated before and failed Mode 2, or passed it and lost the cache comparison in
    ``test_deferred_tools_cache.py``; a Mode 2 failure xfails.
  * candidates: every ``ModelName`` the allowlist doesn't match yet, or the specs in
    ``DAIV_EVAL_DEFERRED_FROZEN_CANDIDATES``. A Mode 2 pass qualifies the model name (without its provider
    slug) for the allowlist; check its prompt cache holds with ``test_deferred_tools_cache.py`` first.

A self-hosted model is gated the same way, through a custom provider::

    DAIV_TEST_PROVIDER_VLLM_BASE_URL=http://gpu-host:8000/v1 DAIV_TEST_PROVIDER_VLLM_API_KEY=... \\
    DAIV_EVAL_DEFERRED_FROZEN_CANDIDATES=vllm:qwen3-coder \\
    uv run pytest --envfile +docker/local/app/config.secrets.env --no-cov \\
        tests/integration_tests/test_deferred_tools_frozen.py -m deferred_frozen
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from automation.agent import BaseAgent
from automation.agent.deferred.index import DeferredToolsIndex
from automation.agent.deferred.prompt import build_deferred_tools_block
from automation.agent.deferred.search_tool import TOOL_SEARCH_NAME, make_tool_search

from .deferred_tools import TOOL_NAME, digest_tool, tool_search_result
from .utils import (
    DEFERRED_FROZEN_ALLOWLISTED,
    DEFERRED_FROZEN_CANDIDATES,
    DEFERRED_FROZEN_REJECTED,
    EVAL_REPEATS,
    require_provider_for_model,
)


def _conversation(*, embed_schema: bool) -> list:
    tool = digest_tool()
    return [
        SystemMessage(content=build_deferred_tools_block(DeferredToolsIndex([tool]))),
        HumanMessage(content="Fetch a digest of ticket ABC-123, at most 3 notes, and skip resolved children."),
        AIMessage(
            content="",
            tool_calls=[
                {"name": TOOL_SEARCH_NAME, "id": "call_ts", "args": {"select": [TOOL_NAME]}, "type": "tool_call"}
            ],
        ),
        ToolMessage(content=tool_search_result(tool, embed_schema=embed_schema), tool_call_id="call_ts"),
    ]


def _bound_model(model_spec: str, *, bind_digest: bool):
    model = BaseAgent.get_model(model=model_spec)
    tools = [make_tool_search(lambda: DeferredToolsIndex([digest_tool()]), top_k_default=5, top_k_max=10)]
    if bind_digest:
        tools.append(digest_tool())
    return model.bind_tools(tools)


def _called_digest_with_typed_args(response: AIMessage) -> bool:
    for call in response.tool_calls or []:
        if call.get("name") != TOOL_NAME:
            continue
        args = call.get("args") or {}
        # A real schema read yields the unguessable param names; string coercions ("3"/"true") count.
        return "note_window" in args or "sweep_closed" in args
    return False


def _describe(response: AIMessage) -> str:
    if response.tool_calls:
        return ", ".join(f"{call['name']}({json.dumps(call['args'], sort_keys=True)})" for call in response.tool_calls)
    return f"text {response.text[:160]!r}"


async def _attempts(model_spec: str, *, bind_digest: bool, embed_schema: bool) -> tuple[list[bool], str]:
    """Whether each of ``EVAL_REPEATS`` attempts called the digest tool with schema-only args, and what each did."""
    model = _bound_model(model_spec, bind_digest=bind_digest)
    conversation = _conversation(embed_schema=embed_schema)
    responses = await asyncio.gather(*(model.ainvoke(conversation) for _ in range(EVAL_REPEATS)))
    report = " | ".join(f"#{i}: {_describe(response)}" for i, response in enumerate(responses, start=1))
    return [_called_digest_with_typed_args(response) for response in responses], report


_MODELS = [
    *(pytest.param(spec, "allowlisted", id=spec) for spec in DEFERRED_FROZEN_ALLOWLISTED),
    *(pytest.param(spec, "rejected", id=spec) for spec in DEFERRED_FROZEN_REJECTED),
    *(pytest.param(spec, "candidate", id=spec) for spec in DEFERRED_FROZEN_CANDIDATES),
]


@pytest.mark.deferred_frozen
@pytest.mark.parametrize("model_spec,group", _MODELS)
async def test_mode1_fallback_reaches_tool(model_spec, group):
    require_provider_for_model(model_spec)
    results, report = await _attempts(model_spec, bind_digest=True, embed_schema=True)
    assert all(results), f"Mode 1 must pass for any model; {model_spec} passed {sum(results)}/{len(results)}: {report}"


@pytest.mark.deferred_frozen
@pytest.mark.parametrize("model_spec,group", _MODELS)
async def test_mode2_frozen_reaches_tool(model_spec, group):
    require_provider_for_model(model_spec)
    results, report = await _attempts(model_spec, bind_digest=False, embed_schema=True)
    if group == "rejected" and not all(results):
        pytest.xfail(f"{model_spec} is a known Mode-2 non-passer ({sum(results)}/{len(results)}): {report}")
    verdict = "do not allowlist it" if group == "candidate" else "remove it from FROZEN_TOOLS_MODELS"
    assert all(results), (
        f"{model_spec} passed Mode 2 (frozen array) {sum(results)}/{len(results)} — {verdict}: {report}"
    )


@pytest.mark.deferred_frozen
@pytest.mark.parametrize("model_spec,group", _MODELS)
async def test_mode3_control_does_not_reach_tool(model_spec, group):
    require_provider_for_model(model_spec)
    results, report = await _attempts(model_spec, bind_digest=False, embed_schema=False)
    assert not any(results), (
        f"Control violated: {model_spec} produced correct args with no schema present "
        f"({sum(results)}/{len(results)} attempts): {report}"
    )
