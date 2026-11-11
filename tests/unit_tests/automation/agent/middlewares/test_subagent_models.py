from __future__ import annotations

import logging
from unittest.mock import Mock, patch

from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from automation.agent.agent_settings import ModelChain
from automation.agent.configured_models import ConfiguredModels
from tests.unit_tests.automation.agent.configured_model_fakes import FakeModels, FakeNotices


class _ToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class _FailingModel(_ToolModel):
    calls: int = 0

    def _generate(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("model not found")


@tool("echo")
def _echo(text: str) -> str:
    """Echo ``text``."""
    return text


ECHO_CALL = {"name": "echo", "args": {"text": "hi"}, "id": "call_echo"}
RUN_MODEL = _ToolModel(messages=iter([]))


def _configured(entries: dict, notices: FakeNotices) -> ConfiguredModels:
    return ConfiguredModels("subagent", entries, chain=ModelChain(names=("fake:run",)), notices=notices)


async def _invoke_subagent(
    configured: ConfiguredModels, name: str, file_spec: str, run_model: _ToolModel = RUN_MODEL
) -> dict:
    """Subagent ``name`` built the way the subagent stacks build it: fallback, then the configured model failover."""
    model = configured.for_subagent(name, file_spec, run_model)
    fallback = _ToolModel(messages=iter([AIMessage(content="from the fallback model")]))
    agent = create_agent(
        model=model, tools=[_echo], middleware=[ModelFallbackMiddleware(fallback), *configured.call_failover(name)]
    )
    return await agent.ainvoke({"messages": [HumanMessage(content="go")]})


class TestSubagentModelFailover:
    async def test_a_configured_model_that_fails_a_call_hands_the_rest_of_the_run_to_the_files_model(self, caplog):
        failing = _FailingModel(messages=iter([]))
        charter = _ToolModel(messages=iter([AIMessage(content="", tool_calls=[ECHO_CALL]), AIMessage(content="done")]))
        notices = FakeNotices()
        configured = _configured({"cr-tests": "fake:configured"}, notices)

        with (
            patch(
                "automation.agent.configured_models.get_model",
                FakeModels(models={"fake:configured": failing, "fake:charter": charter}),
            ),
            caplog.at_level(logging.ERROR, logger="daiv.agent"),
        ):
            result = await _invoke_subagent(configured, "cr-tests", "fake:charter")

        assert result["messages"][-1].content == "done"
        assert failing.calls == 1
        assert [(n.target, n.name, n.configured_model, n.served_model, n.reason) for n in notices.recorded] == [
            ("subagent", "cr-tests", "fake:configured", "fake:charter", "call_failed")
        ]
        assert notices.flushed_with == [1]
        assert "fake:configured" in caplog.text

    async def test_the_runs_model_serves_a_subagent_whose_file_names_no_model(self):
        failing = _FailingModel(messages=iter([]))
        notices = FakeNotices()
        configured = _configured({"general-purpose": "fake:configured"}, notices)
        run_model = _ToolModel(messages=iter([AIMessage(content="from the run model")]))

        with patch("automation.agent.configured_models.get_model", FakeModels(models={"fake:configured": failing})):
            result = await _invoke_subagent(configured, "general-purpose", "", run_model)

        assert result["messages"][-1].content == "from the run model"
        assert [n.served_model for n in notices.recorded] == ["fake:run"]

    async def test_another_subagent_on_the_failed_model_skips_it_and_is_reported_too(self):
        failing = _FailingModel(messages=iter([]))
        notices = FakeNotices()
        configured = _configured({"cr-tests": "fake:configured", "cr-security": "fake:configured"}, notices)
        models = FakeModels(
            models={
                "fake:configured": failing,
                "fake:charter": _ToolModel(messages=iter([AIMessage(content="done")] * 2)),
            }
        )

        with patch("automation.agent.configured_models.get_model", models):
            await _invoke_subagent(configured, "cr-tests", "fake:charter")
            await _invoke_subagent(configured, "cr-security", "fake:charter")

        assert failing.calls == 1
        assert [(n.name, n.reason) for n in notices.recorded] == [
            ("cr-tests", "call_failed"),
            ("cr-security", "call_failed"),
        ]

    def test_a_subagent_without_an_entry_gets_no_failover(self):
        configured = _configured({"cr-tests": "fake:configured"}, FakeNotices())

        with patch("automation.agent.configured_models.get_model", FakeModels("fake:configured", "fake:charter")):
            configured.for_subagent("cr-security", "fake:charter", Mock())

        assert configured.call_failover("cr-security") == []
