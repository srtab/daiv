from types import SimpleNamespace

from deepagents.backends import StateBackend
from deepagents.middleware.summarization import create_summarization_middleware
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from automation.agent.middlewares.summarization import build_summarization_middleware

LARGE_CONTENT = "x" * 3000


def _conversation(turns: int) -> list:
    messages = [HumanMessage("Write the module", id="h0")]
    for i in range(turns):
        content = LARGE_CONTENT if i == 0 else "small"
        messages.append(
            AIMessage(
                "",
                id=f"a{i}",
                tool_calls=[{"id": f"t{i}", "name": "write_file", "args": {"file_path": "/f", "content": content}}],
            )
        )
        messages.append(ToolMessage("ok", tool_call_id=f"t{i}", id=f"tm{i}"))
    return messages


async def _first_write_as_sent(middleware) -> str:
    """The ``content`` argument of the conversation's first tool call, as the model receives it."""
    messages = _conversation(turns=15)
    request = ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=messages,
        state={"messages": messages},
        runtime=SimpleNamespace(context=None),
    )
    sent: list = []

    async def handler(req: ModelRequest) -> ModelResponse:
        sent.extend(req.messages)
        return ModelResponse(result=[AIMessage("done")])

    await middleware.awrap_model_call(request, handler)
    [first_call] = next(m for m in sent if m.id == "a0").tool_calls
    return first_call["args"]["content"]


async def test_old_tool_arguments_reach_the_model_unchanged():
    middleware = build_summarization_middleware(FakeListChatModel(responses=["ok"]), StateBackend())

    assert await _first_write_as_sent(middleware) == LARGE_CONTENT


async def test_deepagents_default_clips_the_same_conversation():
    middleware = create_summarization_middleware(FakeListChatModel(responses=["ok"]), StateBackend())

    assert await _first_write_as_sent(middleware) != LARGE_CONTENT


def test_takes_the_slot_of_deepagents_default_by_name():
    middleware = build_summarization_middleware(FakeListChatModel(responses=["ok"]), StateBackend())

    assert middleware.name == create_summarization_middleware(FakeListChatModel(responses=["ok"]), StateBackend()).name
