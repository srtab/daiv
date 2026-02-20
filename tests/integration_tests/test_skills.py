import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import testing as t

from automation.agent.graph import create_daiv_agent
from codebase.base import Scope
from codebase.context import set_runtime_ctx

from .utils import CODING_MODEL_NAMES, INTERRUPT_ALL_TOOLS_CONFIG

TEST_SUITE = "DAIV: Skills"


@pytest.mark.skills
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", CODING_MODEL_NAMES)
@pytest.mark.parametrize(
    "inputs",
    [
        pytest.param(
            {"user_message": "Plan an implementation for echo slash command", "skill": "plan"},
            id="plan-skill-triggered-by-user-intent",
        ),
        pytest.param(
            {"user_message": "/plan implement echo slash command", "skill": "plan"},
            id="plan-skill-triggered-by-slash-command",
        ),
        pytest.param(
            {"user_message": "/plan address the issue #123", "skill": "plan"},
            id="plan-skill-triggered-by-slash-command-with-issue-reference",
        ),
    ],
)
async def test_skill_activated(model_name, inputs):
    t.log_inputs({"model_name": model_name, "inputs": inputs})

    async with set_runtime_ctx(repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main") as ctx:
        agent = await create_daiv_agent(
            ctx=ctx,
            model_names=[model_name],
            auto_commit_changes=False,
            interrupt_on=INTERRUPT_ALL_TOOLS_CONFIG,
            checkpointer=InMemorySaver(),
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": inputs["user_message"]}]},
            context=ctx,
            config={"configurable": {"thread_id": "1"}},
        )

        t.log_outputs(result)

        assert result["messages"][-1].tool_calls, result["messages"][-1].pretty_print()
        assert any(tool_call["name"] == "skill" for tool_call in result["messages"][-1].tool_calls), result["messages"][
            -1
        ].pretty_print()
        assert any(
            tool_call["args"]["skill"] == inputs["skill"]
            for tool_call in result["messages"][-1].tool_calls
            if tool_call["name"] == "skill"
        ), result["messages"][-1].pretty_print()
