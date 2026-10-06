import pytest
from langsmith import testing as t

from automation.agent.constants import SKILLS_TOOL_NAME

from .prompt_leak import assert_no_prompt_leak
from .utils import (
    INTERRUPT_ALL_TOOLS_CONFIG,
    SKILLS_MODELS,
    extract_tool_calls,
    measure,
    require_provider_for_model,
    run_agent_once,
)

TEST_SUITE = "DAIV: Skills"

SKILL_REQUESTS = [
    pytest.param("Plan an implementation for echo slash command", "plan", id="plan-skill-triggered-by-user-intent"),
    pytest.param("/plan implement echo slash command", "plan", id="plan-skill-triggered-by-slash-command"),
    pytest.param(
        "/plan address the issue #123", "plan", id="plan-skill-triggered-by-slash-command-with-issue-reference"
    ),
    pytest.param("Create an AGENTS.md for this repository", "init", id="init-skill-triggered-by-user-intent"),
    pytest.param("/init", "init", id="init-skill-triggered-by-slash-command"),
    pytest.param("Analyze this repo and generate agent docs", "init", id="init-skill-triggered-by-analyze-phrase"),
]

NEAR_MISS_REQUESTS = [
    pytest.param("Review the docstring of `create_daiv_agent` and tell me what it returns", id="review-a-docstring"),
    pytest.param("What's in AGENTS.md?", id="read-agents-md"),
    pytest.param("Is the GitLab webhook endpoint authenticated?", id="is-endpoint-authenticated"),
    pytest.param('Fix the typo "recieve" in README.md', id="fix-a-typo"),
    pytest.param("What does the docs folder say about plans?", id="plan-in-docs"),
]

assert_no_prompt_leak([param.values[0] for param in [*SKILL_REQUESTS, *NEAR_MISS_REQUESTS]])


@pytest.mark.skills
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", SKILLS_MODELS)
@pytest.mark.parametrize("user_message,skill", SKILL_REQUESTS)
async def test_skill_activated(model_name, user_message, skill, request):
    require_provider_for_model(model_name)

    t.log_inputs({"model_name": model_name, "user_message": user_message, "skill": skill})

    with measure(request) as metrics:
        result = await run_agent_once(model_name, user_message, interrupt_on=INTERRUPT_ALL_TOOLS_CONFIG)
    metrics.messages = result["messages"]

    t.log_outputs(result)

    tool_calls = extract_tool_calls(result["messages"])

    assert tool_calls, "Expected tool calls, but got none"
    assert any(tool_call["name"] == SKILLS_TOOL_NAME for tool_call in tool_calls), (
        f"Expected skill tool call, but got {tool_calls}"
    )
    assert any(
        tool_call["args"]["skill"] == skill for tool_call in tool_calls if tool_call["name"] == SKILLS_TOOL_NAME
    ), f"Expected skill tool call with the skill name '{skill}', but got {tool_calls}"


@pytest.mark.skills
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", SKILLS_MODELS)
@pytest.mark.parametrize("user_message", NEAR_MISS_REQUESTS)
async def test_skill_not_activated(model_name, user_message, request):
    require_provider_for_model(model_name)

    t.log_inputs({"model_name": model_name, "user_message": user_message})

    with measure(request) as metrics:
        result = await run_agent_once(model_name, user_message, interrupt_on=INTERRUPT_ALL_TOOLS_CONFIG)
    metrics.messages = result["messages"]

    t.log_outputs(result)

    # tool_search and ask_user_question are not interrupted, so the run can outlast its first turn.
    skill_calls = [call for call in extract_tool_calls(result["messages"]) if call["name"] == SKILLS_TOOL_NAME]
    assert not skill_calls, f"Expected no skill call for a request no skill covers, got {skill_calls}"
