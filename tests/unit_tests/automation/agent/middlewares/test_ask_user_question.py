from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from automation.agent.middlewares.ask_user_question import AskUserQuestionMiddleware, ask_user_question
from automation.agent.prompts import ASK_USER_DISABLED_SYSTEM_PROMPT, ASK_USER_QUESTION_SYSTEM_PROMPT
from automation.agent.questions import (
    ASK_USER_QUESTION_TOOL_NAME,
    NOT_ALONE_ERROR,
    QUESTION_DELIVERED,
    pending_question,
    render_questions,
)
from tests.unit_tests.conftest import SAMPLE_QUESTION_PAYLOAD


class _ToolModel(GenericFakeChatModel):
    """A scripted model that accepts ``bind_tools`` so ``create_agent`` can run it."""

    def bind_tools(self, tools, **kwargs):
        return self


class _AfterAgentProbe(AgentMiddleware):
    def __init__(self):
        super().__init__()
        self.ran = False

    async def aafter_agent(self, state, runtime):
        self.ran = True


def _ask(call_id, payload=SAMPLE_QUESTION_PAYLOAD):
    return {"id": call_id, "name": ASK_USER_QUESTION_TOOL_NAME, "args": payload}


async def _run(*scripted):
    probe = _AfterAgentProbe()
    agent = create_agent(
        model=_ToolModel(messages=iter(scripted)), tools=[], middleware=[AskUserQuestionMiddleware(), probe]
    )
    result = await agent.ainvoke({"messages": [HumanMessage(content="migrate the database")]})
    return result["messages"], probe


async def test_a_valid_question_ends_the_turn_and_after_agent_still_runs():
    messages, probe = await _run(AIMessage(content="", tool_calls=[_ask("ask-1")]))

    assert messages[-2].content == QUESTION_DELIVERED
    assert messages[-1].content == render_questions(SAMPLE_QUESTION_PAYLOAD)
    assert pending_question(messages) == SAMPLE_QUESTION_PAYLOAD
    assert probe.ran is True


async def test_invalid_arguments_return_an_error_and_the_loop_continues():
    bad = {"questions": [{**SAMPLE_QUESTION_PAYLOAD["questions"][0], "header": "x" * 13}]}
    messages, _probe = await _run(
        AIMessage(content="", tool_calls=[_ask("ask-1", bad)]), AIMessage(content="", tool_calls=[_ask("ask-2")])
    )

    errors = [m for m in messages if isinstance(m, ToolMessage) and m.status == "error"]
    assert [m.tool_call_id for m in errors] == ["ask-1"]
    assert pending_question(messages) == SAMPLE_QUESTION_PAYLOAD


async def test_a_call_with_siblings_is_refused_and_nothing_is_asked():
    messages, _probe = await _run(
        AIMessage(content="", tool_calls=[_ask("ask-1"), _ask("ask-2")]), AIMessage(content="I will decide myself.")
    )

    refused = [m for m in messages if isinstance(m, ToolMessage)]
    assert {m.content for m in refused} == {NOT_ALONE_ERROR}
    assert {m.status for m in refused} == {"error"}
    assert messages[-1].content == "I will decide myself."
    assert pending_question(messages) is None


def test_the_tool_schema_carries_the_bounds():
    schema = ask_user_question.tool_call_schema.model_json_schema()

    assert schema["properties"]["questions"]["minItems"] == 1
    assert schema["properties"]["questions"]["maxItems"] == 4
    assert "runtime" not in schema["properties"]


def _request():
    # If this langchain's ModelRequest rejects ``system_prompt=``, pass ``system_message=SystemMessage("BASE")``.
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([])), messages=[HumanMessage(content="hi")], system_prompt="BASE"
    )


async def test_enabled_binds_the_tool_and_adds_the_asking_section():
    seen = []

    async def handler(request):
        seen.append(request.system_prompt)
        return AIMessage(content="ok")

    middleware = AskUserQuestionMiddleware()
    await middleware.awrap_model_call(_request(), handler)

    assert [tool.name for tool in middleware.tools] == [ASK_USER_QUESTION_TOOL_NAME]
    assert seen == [f"BASE\n\n{ASK_USER_QUESTION_SYSTEM_PROMPT}"]


async def test_disabled_binds_nothing_and_tells_the_model_nobody_can_answer():
    seen = []

    async def handler(request):
        seen.append(request.system_prompt)
        return AIMessage(content="ok")

    middleware = AskUserQuestionMiddleware(enabled=False)
    await middleware.awrap_model_call(_request(), handler)

    assert middleware.tools == []
    assert seen == [f"BASE\n\n{ASK_USER_DISABLED_SYSTEM_PROMPT}"]
