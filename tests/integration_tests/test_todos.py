import pytest
from langsmith import testing as t

from .coverage_grading import todo_planning_violation
from .prompt_leak import assert_no_prompt_leak
from .utils import TODOS_MODELS, extract_tool_calls, measure, require_provider_for_model, run_agent_once

pytestmark = [
    pytest.mark.todos,
    pytest.mark.langsmith(test_suite_name="DAIV: Todos"),
    pytest.mark.parametrize("model_name", TODOS_MODELS),
]

MULTI_STEP_REQUEST = (
    "Add a `--dry-run` option to the `release_orphan_queued_sessions` management command: add the argument, "
    "skip the status update and the enqueue when it is set, count the runs it would release, include that count "
    "in the summary it prints, add tests for both modes, and mention the option in the command's help text."
)
ONE_STEP_REQUEST = "Add `*.log` to `.gitignore`."

assert_no_prompt_leak([MULTI_STEP_REQUEST, ONE_STEP_REQUEST])

WRITE_INTERRUPTS = {"edit_file": True, "write_file": True, "bash": True, "task": True}


@pytest.fixture(autouse=True)
def _require_provider(model_name):
    require_provider_for_model(model_name)


async def _run(model_name: str, prompt: str, request: pytest.FixtureRequest) -> list:
    t.log_inputs({"model_name": model_name, "prompt": prompt})
    with measure(request) as metrics:
        result = await run_agent_once(model_name, prompt, interrupt_on=WRITE_INTERRUPTS, ask_user_enabled=False)
    metrics.messages = result["messages"]
    t.log_outputs(result)
    return extract_tool_calls(result["messages"])


async def test_a_multi_step_request_is_planned_before_the_first_edit(model_name, request):
    tool_calls = await _run(model_name, MULTI_STEP_REQUEST, request)

    assert (violation := todo_planning_violation(tool_calls)) is None, violation


async def test_a_one_step_edit_is_not_planned(model_name, request):
    tool_calls = await _run(model_name, ONE_STEP_REQUEST, request)

    todos = [call for call in tool_calls if call["name"] == "write_todos"]
    assert not todos, f"Expected no write_todos for a one-step edit, got {todos}"
