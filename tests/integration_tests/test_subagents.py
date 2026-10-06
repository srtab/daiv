import pytest
from langsmith import testing as t
from sandbox_envs.services import build_sandbox_spec

from automation.agent.agent_settings import ModelChain
from automation.agent.constants import REPO_PATH
from automation.agent.middlewares.file_system import READ_ONLY_FS_TOOLS
from automation.agent.subagents import create_explore_subagent
from automation.agent.workspace.disk import DiskWorkspace
from codebase.base import Scope
from codebase.context import set_runtime_ctx

from .coverage_grading import absolute_paths, calls_outside
from .prompt_leak import assert_no_prompt_leak
from .utils import SUBAGENTS_MODELS, extract_tool_calls, final_text, measure, require_provider_for_model

pytestmark = [
    pytest.mark.subagents,
    pytest.mark.langsmith(test_suite_name="DAIV: Subagents"),
    pytest.mark.parametrize("model_name", SUBAGENTS_MODELS),
]

SEARCH_REQUEST = "Find where the step budget is enforced. Thoroughness: medium."

assert_no_prompt_leak([SEARCH_REQUEST])

EXPLORE_TOOLS = frozenset({*READ_ONLY_FS_TOOLS, "write_todos"})


async def test_explore_reports_absolute_paths_using_only_its_own_tools(model_name, eval_request):
    require_provider_for_model(model_name)
    t.log_inputs({"model_name": model_name, "prompt": SEARCH_REQUEST})

    async with set_runtime_ctx(
        repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main", sandbox_spec=await build_sandbox_spec(None)
    ) as ctx:
        explore = create_explore_subagent(DiskWorkspace(ctx), f"{REPO_PATH}/", models=ModelChain(names=(model_name,)))
        with measure(eval_request) as metrics:
            result = await explore["runnable"].ainvoke(
                {"messages": [{"role": "user", "content": SEARCH_REQUEST}]}, context=ctx
            )
    messages = metrics.messages = result["messages"]
    t.log_outputs(result)

    foreign = calls_outside(extract_tool_calls(messages), EXPLORE_TOOLS)
    assert not foreign, f"explore called tools it does not have: {foreign}"
    report = final_text(messages)
    assert absolute_paths(report, REPO_PATH), f"Expected {REPO_PATH}/ paths in the report, got: {report[-800:]!r}"
