import logging
from unittest.mock import Mock

import pytest
from deepagents.middleware.subagents import CompiledSubAgent, SubAgentMiddleware
from langchain.agents import create_agent
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolCallRequest

from automation.agent.middlewares.logging import ToolCallLoggingMiddleware

_NO_RUNTIME = object()
"""Parametrize sentinel for "the tool call has no runtime at all" (`runtime=None`)."""


@tool
def ping(x: str) -> str:
    """Ping."""
    return f"pong {x}"


class FakeToolModel(GenericFakeChatModel):
    """
    Scripted chat model for the contract tests below.

    `GenericFakeChatModel` doesn't implement `bind_tools`; returning `self` keeps the
    scripted message iterator on the model instance `create_agent` actually invokes.
    """

    def bind_tools(self, tools, **kwargs):
        return self


class TestToolCallLoggingMiddleware:
    async def test_logs_tool_call(self, caplog):
        caplog.set_level(logging.INFO, logger="daiv.tools")

        request = ToolCallRequest(
            tool_call={"name": "demo_tool", "args": {"x": 1}, "id": "call_1"},
            tool=None,
            state={"messages": []},
            runtime=Mock(),
        )

        async def handler(req: ToolCallRequest):
            return ToolMessage(content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"])

        result = await ToolCallLoggingMiddleware().awrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.content == "ok"

        messages = [r.getMessage() for r in caplog.records if r.name == "daiv.tools"]
        assert any("[demo_tool] Tool call (id=call_1" in m for m in messages)

    async def test_logs_agent_name_from_runtime_metadata(self, caplog):
        caplog.set_level(logging.INFO, logger="daiv.tools")

        runtime = Mock()
        runtime.config = {"metadata": {"lc_agent_name": "explore"}}
        request = ToolCallRequest(
            tool_call={"name": "demo_tool", "args": {"x": 1}, "id": "call_1"},
            tool=None,
            state={"messages": []},
            runtime=runtime,
        )

        async def handler(req: ToolCallRequest):
            return ToolMessage(content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"])

        await ToolCallLoggingMiddleware().awrap_tool_call(request, handler)

        messages = [r.getMessage() for r in caplog.records if r.name == "daiv.tools"]
        assert any("[explore] [demo_tool] Tool call (id=call_1" in m for m in messages)

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(_NO_RUNTIME, id="no-runtime"),
            pytest.param(None, id="config-none"),
            pytest.param("not-a-dict", id="config-not-a-dict"),
            pytest.param({}, id="metadata-absent"),
            pytest.param({"metadata": {}}, id="agent-name-absent"),
            pytest.param({"metadata": {"lc_agent_name": ""}}, id="agent-name-empty"),
        ],
    )
    async def test_logs_unknown_agent_when_name_unavailable(self, caplog, config):
        caplog.set_level(logging.INFO, logger="daiv.tools")

        runtime = None if config is _NO_RUNTIME else Mock(config=config)
        request = ToolCallRequest(
            tool_call={"name": "demo_tool", "args": {"x": 1}, "id": "call_1"},
            tool=None,
            state={"messages": []},
            runtime=runtime,
        )

        async def handler(req: ToolCallRequest):
            return ToolMessage(content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"])

        await ToolCallLoggingMiddleware().awrap_tool_call(request, handler)

        messages = [r.getMessage() for r in caplog.records if r.name == "daiv.tools"]
        assert any("[<unknown-agent>] [demo_tool] Tool call (id=call_1" in m for m in messages)

    async def test_agent_name_resolves_in_real_agent_run(self, caplog):
        """
        Contract test for the langchain side: `create_agent(name=...)` must surface the name
        where `_agent_name()` reads it (`metadata["lc_agent_name"]`). Uses a real agent run so
        a langchain upgrade that renames the internal key fails here instead of silently
        degrading the log lines to `<unknown-agent>`. The deepagents subagent path is pinned
        separately by `test_subagent_name_resolves_through_deepagents_task_tool`.
        """
        caplog.set_level(logging.INFO, logger="daiv.tools")

        model = FakeToolModel(
            messages=iter([
                AIMessage(content="", tool_calls=[{"name": "ping", "args": {"x": "1"}, "id": "call_1"}]),
                AIMessage(content="done"),
            ])
        )
        agent = create_agent(model=model, tools=[ping], middleware=[ToolCallLoggingMiddleware()], name="contract-agent")

        await agent.ainvoke({"messages": [("user", "go")]})

        messages = [r.getMessage() for r in caplog.records if r.name == "daiv.tools"]
        assert any("[contract-agent] [ping] Tool call (id=call_1" in m for m in messages)

    async def test_subagent_name_resolves_through_deepagents_task_tool(self, caplog):
        """
        Contract test for the deepagents side: a subagent's tool calls must log under the
        *subagent's* name, not the parent's. Pins the upstream behavior a deepagents upgrade
        could silently break: the task tool invokes the subagent without propagating the
        parent's metadata over it, so the subagent's own `lc_agent_name` (baked in by
        `create_agent(name=...)` / `SubAgentMiddleware`) wins inside the subagent run.
        """
        caplog.set_level(logging.INFO, logger="daiv.tools")

        sub_model = FakeToolModel(
            messages=iter([
                AIMessage(content="", tool_calls=[{"name": "ping", "args": {"x": "1"}, "id": "call_1"}]),
                AIMessage(content="sub done"),
            ])
        )
        subagent = create_agent(
            model=sub_model, tools=[ping], middleware=[ToolCallLoggingMiddleware()], name="contract-subagent"
        )

        parent_model = FakeToolModel(
            messages=iter([
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"description": "go", "subagent_type": "contract-subagent"},
                            "id": "call_2",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ])
        )
        parent = create_agent(
            model=parent_model,
            tools=[],
            middleware=[
                SubAgentMiddleware(
                    # backend is only consulted for dict-spec subagents, not CompiledSubAgent
                    backend=Mock(),
                    subagents=[
                        CompiledSubAgent(name="contract-subagent", description="Contract subagent.", runnable=subagent)
                    ],
                ),
                ToolCallLoggingMiddleware(),
            ],
            name="contract-agent",
        )

        await parent.ainvoke({"messages": [("user", "go")]})

        messages = [r.getMessage() for r in caplog.records if r.name == "daiv.tools"]
        assert any("[contract-agent] [task] Tool call (id=call_2" in m for m in messages)
        assert any("[contract-subagent] [ping] Tool call (id=call_1" in m for m in messages)

    async def test_logs_tool_call_exception_and_reraises(self, caplog):
        caplog.set_level(logging.INFO, logger="daiv.tools")

        request = ToolCallRequest(
            tool_call={"name": "demo_tool", "args": {"x": 1}, "id": "call_1"},
            tool=None,
            state={"messages": []},
            runtime=Mock(),
        )

        async def handler(_req: ToolCallRequest):
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await ToolCallLoggingMiddleware().awrap_tool_call(request, handler)

        messages = [r.getMessage() for r in caplog.records if r.name == "daiv.tools"]
        assert any("[demo_tool] Tool call (id=call_1" in m for m in messages)
