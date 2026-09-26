import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import testing as t

from automation.agent.graph import create_daiv_agent
from automation.agent.questions import pending_question
from codebase.base import Scope
from codebase.context import set_runtime_ctx

from .utils import CODING_MODEL_NAMES, require_provider_for_model

AMBIGUOUS_PROMPT = "Migrate this project to a different database engine."


@pytest.mark.ask_user
@pytest.mark.langsmith(test_suite_name="DAIV: Ask user question")
@pytest.mark.parametrize("model_name", CODING_MODEL_NAMES)
async def test_an_ambiguous_request_ends_on_a_question(model_name):
    require_provider_for_model(model_name)
    t.log_inputs({"model_name": model_name, "prompt": AMBIGUOUS_PROMPT})

    async with set_runtime_ctx(repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main") as ctx:
        agent = await create_daiv_agent(
            ctx=ctx,
            model_names=[model_name],
            auto_commit_changes=False,
            checkpointer=InMemorySaver(),
            sandbox_enabled=False,
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": AMBIGUOUS_PROMPT}]},
            context=ctx,
            config={"configurable": {"thread_id": "1"}},
        )

    t.log_outputs(result)
    assert pending_question(result["messages"]) is not None, "Expected the run to end on a question"
