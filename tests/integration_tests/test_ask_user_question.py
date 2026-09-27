import logging
from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import testing as t

from automation.agent.graph import create_daiv_agent
from automation.agent.questions import ASK_USER_QUESTION_TOOL_NAME, pending_question, render_questions
from codebase.base import Scope
from codebase.context import set_runtime_ctx

from .evaluators import judge_question_relevance
from .utils import ASK_USER_MODELS, extract_tool_calls, require_provider_for_model

logger = logging.getLogger(__name__)

TEST_SUITE = "DAIV: Ask user question"

EDIT_TOOL_NAMES = {"write_file", "edit_file"}

MIGRATION_PROMPT = "Migrate this project to a different database engine."
MIGRATION_ANSWER = (
    "Target MySQL 8. Keep the existing data; a short downtime window is fine. "
    "Don't edit any files yet: reply with a short migration plan."
)
SKIP_ANSWER = "Skip these questions — use your best judgment and state the assumptions you made."


@asynccontextmanager
async def agent_runner(model_name: str, *, ask_user_enabled: bool = True):
    async with set_runtime_ctx(repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main") as ctx:
        agent = await create_daiv_agent(
            ctx=ctx,
            model_names=[model_name],
            auto_commit_changes=False,
            checkpointer=InMemorySaver(),
            sandbox_enabled=False,
            ask_user_enabled=ask_user_enabled,
        )

        async def run(content: str) -> dict:
            return await agent.ainvoke(
                {"messages": [{"role": "user", "content": content}]},
                context=ctx,
                config={"configurable": {"thread_id": "1"}},
            )

        yield run


def ask_calls(messages) -> list:
    return [call for call in extract_tool_calls(messages) if call["name"] == ASK_USER_QUESTION_TOOL_NAME]


def failed_asks(messages) -> list[str]:
    return [
        str(message.content)
        for message in messages
        if isinstance(message, ToolMessage)
        and message.name == ASK_USER_QUESTION_TOOL_NAME
        and message.status == "error"
    ]


def final_text(messages) -> str:
    return messages[-1].text if messages and isinstance(messages[-1], AIMessage) else ""


def assert_ended_on_one_clean_question(messages) -> dict:
    payload = pending_question(messages)
    assert payload is not None, "Expected the run to end on a question"
    assert not (errors := failed_asks(messages)), f"Expected every ask to be valid on the first try, got: {errors}"
    assert len(ask_calls(messages)) == 1, f"Expected one batched ask, got {len(ask_calls(messages))}"
    edits = [call["name"] for call in extract_tool_calls(messages) if call["name"] in EDIT_TOOL_NAMES]
    assert not edits, f"Expected no file edits before asking, got: {edits}"
    return payload


async def log_question_relevance(prompt: str, payload: dict) -> None:
    verdict = await judge_question_relevance(prompt, render_questions(payload))
    logger.info("question_relevance=%s: %s", verdict.passed, verdict.explanation)
    t.log_feedback(key="question_relevance", score=int(verdict.passed), comment=verdict.explanation)


@pytest.mark.ask_user
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", ASK_USER_MODELS)
@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param(MIGRATION_PROMPT, id="readings-lead-to-different-work"),
        pytest.param("Rename the product to its new name everywhere in the codebase.", id="blocked-on-missing-fact"),
    ],
)
async def test_an_ambiguous_request_ends_on_a_question(model_name, prompt):
    require_provider_for_model(model_name)
    t.log_inputs({"model_name": model_name, "prompt": prompt})

    async with agent_runner(model_name) as run:
        result = await run(prompt)

    t.log_outputs(result)
    payload = assert_ended_on_one_clean_question(result["messages"])
    await log_question_relevance(prompt, payload)


@pytest.mark.ask_user
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", ASK_USER_MODELS)
@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param("Which test framework does this project use?", id="answerable-by-reading-the-code"),
        pytest.param("Add `.DS_Store` to `.gitignore`.", id="clear-scoped-edit"),
    ],
)
async def test_a_clear_request_is_not_met_with_a_question(model_name, prompt):
    require_provider_for_model(model_name)
    t.log_inputs({"model_name": model_name, "prompt": prompt})

    async with agent_runner(model_name) as run:
        result = await run(prompt)

    t.log_outputs(result)
    assert not ask_calls(result["messages"]), f"Expected no question, got: {ask_calls(result['messages'])}"
    assert final_text(result["messages"]), "Expected the run to end on a reply"


@pytest.mark.ask_user
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", ASK_USER_MODELS)
async def test_an_answer_resumes_the_run(model_name):
    require_provider_for_model(model_name)
    t.log_inputs({"model_name": model_name, "prompt": MIGRATION_PROMPT, "answer": MIGRATION_ANSWER})

    async with agent_runner(model_name) as run:
        asked = await run(MIGRATION_PROMPT)
        assert_ended_on_one_clean_question(asked["messages"])
        answered = await run(MIGRATION_ANSWER)

    t.log_outputs(answered)
    answer_turn = answered["messages"][len(asked["messages"]) :]
    assert pending_question(answered["messages"]) is None, "Expected the answer to clear the pending question"
    assert not ask_calls(answer_turn), f"Expected no further question, got: {ask_calls(answer_turn)}"
    assert final_text(answered["messages"]), "Expected the run to end on a reply"


@pytest.mark.ask_user
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", ASK_USER_MODELS)
async def test_a_skipped_question_is_not_asked_again(model_name):
    require_provider_for_model(model_name)
    t.log_inputs({"model_name": model_name, "prompt": MIGRATION_PROMPT, "answer": SKIP_ANSWER})

    async with agent_runner(model_name) as run:
        asked = await run(MIGRATION_PROMPT)
        assert_ended_on_one_clean_question(asked["messages"])
        skipped = await run(SKIP_ANSWER)

    t.log_outputs(skipped)
    skip_turn = skipped["messages"][len(asked["messages"]) :]
    assert not ask_calls(skip_turn), f"Expected the skip to be honoured, got: {ask_calls(skip_turn)}"
    assert final_text(skipped["messages"]), "Expected the run to end on a reply"
