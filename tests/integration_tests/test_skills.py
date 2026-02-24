import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import testing as t

from automation.agent.graph import create_daiv_agent
from codebase.base import Scope
from codebase.context import set_runtime_ctx

from .utils import CODING_MODEL_NAMES, INTERRUPT_ALL_TOOLS_CONFIG, extract_tool_calls

TEST_SUITE = "DAIV: Skills"


@pytest.mark.skills
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", CODING_MODEL_NAMES)
@pytest.mark.parametrize(
    "user_message, skill",
    [
        pytest.param("Plan an implementation for echo slash command", "plan", id="plan-skill-triggered-by-user-intent"),
        pytest.param("/plan implement echo slash command", "plan", id="plan-skill-triggered-by-slash-command"),
        pytest.param(
            "/plan address the issue #123", "plan", id="plan-skill-triggered-by-slash-command-with-issue-reference"
        ),
        pytest.param(
            {"user_message": "Create an AGENTS.md for this repository", "skill": "init"},
            id="init-skill-triggered-by-user-intent",
        ),
        pytest.param({"user_message": "/init", "skill": "init"}, id="init-skill-triggered-by-slash-command"),
        pytest.param(
            {"user_message": "Analyze this repo and generate agent docs", "skill": "init"},
            id="init-skill-triggered-by-analyze-phrase",
        ),
    ],
)
async def test_skill_activated(model_name, user_message, skill):
    t.log_inputs({"model_name": model_name, "user_message": user_message, "skill": skill})

    async with set_runtime_ctx(repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main") as ctx:
        agent = await create_daiv_agent(
            ctx=ctx,
            model_names=[model_name],
            auto_commit_changes=False,
            checkpointer=InMemorySaver(),
            interrupt_on=INTERRUPT_ALL_TOOLS_CONFIG,
            sandbox_enabled=False,
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_message}]},
            context=ctx,
            config={"configurable": {"thread_id": "1"}},
        )

        t.log_outputs(result)

        tool_calls = extract_tool_calls(result["messages"])

        assert tool_calls, "Expected tool calls, but got none"
        assert any(tool_call["name"] == "skill" for tool_call in tool_calls), (
            f"Expected skill tool call, but got {tool_calls}"
        )
        assert any(tool_call["args"]["skill"] == skill for tool_call in tool_calls if tool_call["name"] == "skill"), (
            f"Expected skill tool call with the skill name '{skill}', but got {tool_calls}"
        )
