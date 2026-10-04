from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from automation.agent.middlewares.reminders import call_with_reminder, persist_reminder
from automation.agent.synthetic import SYNTHETIC_KWARG, is_synthetic, synthetic_message


def _request() -> ModelRequest:
    return ModelRequest(model=GenericFakeChatModel(messages=iter([])), messages=[HumanMessage(content="hi")])


def test_persist_reminder_saves_the_reminder_ahead_of_the_reply():
    reminder = synthetic_message("<system-reminder>x</system-reminder>", kind="step_budget")
    reply = AIMessage(content="ok")
    response = ModelResponse(result=[reply], structured_response={"answer": 42})

    persisted = persist_reminder(response, reminder)

    assert persisted.result == [reminder, reply]
    assert persisted.structured_response == {"answer": 42}


async def test_call_with_reminder_sends_and_saves_the_same_message():
    seen: list[ModelRequest] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request)
        return ModelResponse(result=[AIMessage(content="ok")])

    request = _request()
    response = await call_with_reminder(request, handler, "<system-reminder>x</system-reminder>", kind="loop_breaker")

    sent = seen[0].messages[-1]
    assert sent.content == "<system-reminder>x</system-reminder>"
    assert sent.additional_kwargs == {SYNTHETIC_KWARG: "loop_breaker"}
    assert response.result[0] is sent
    assert is_synthetic(response.result[0])
    assert response.result[1].content == "ok"
    assert len(request.messages) == 1
