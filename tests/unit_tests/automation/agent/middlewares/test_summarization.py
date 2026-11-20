from types import SimpleNamespace
from unittest.mock import patch

import pytest
from deepagents.backends import StateBackend
from deepagents.middleware import SummarizationMiddleware
from deepagents.middleware.summarization import compute_summarization_defaults
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from automation.agent.middlewares.summarization import (
    COMPACTION_WINDOW_FRACTION,
    MAX_IMAGES_BEFORE_COMPACTION,
    build_summarization_middleware,
)
from automation.agent.usage_tracking import ResolvedWindow

LARGE_CONTENT = "x" * 3000
MODULE = "automation.agent.middlewares.summarization"


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


async def test_old_tool_arguments_reach_the_model_unchanged_when_the_window_is_known():
    model = FakeListChatModel(responses=["ok"], profile={"max_input_tokens": 200_000})

    assert await _first_write_as_sent(build_summarization_middleware(model, StateBackend())) == LARGE_CONTENT


async def test_the_conversation_is_old_enough_to_be_truncated():
    truncating = SummarizationMiddleware(
        model=FakeListChatModel(responses=["ok"]),
        backend=StateBackend(),
        truncate_args_settings={"trigger": ("messages", 20), "keep": ("messages", 20)},
    )

    assert await _first_write_as_sent(truncating) != LARGE_CONTENT


def _built_with(model, catalog_window: int | None) -> dict:
    """The arguments the builder passes to deepagents' summarization, with the catalog lookup stubbed."""
    resolved = ResolvedWindow(catalog_window, "genai_prices") if catalog_window else None
    with (
        patch(f"{MODULE}.resolve_window_by_name", return_value=resolved),
        patch(f"{MODULE}.SummarizationMiddleware") as summarization,
    ):
        build_summarization_middleware(model, StateBackend())
    return summarization.call_args.kwargs


def test_a_profiled_window_keeps_deepagents_trigger_without_truncation():
    model = FakeListChatModel(responses=["ok"], profile={"max_input_tokens": 200_000})

    kwargs = _built_with(model, catalog_window=None)

    assert kwargs["trigger"] == compute_summarization_defaults(model)["trigger"]
    assert kwargs["truncate_args_settings"] is None


def test_a_catalog_window_below_the_fixed_trigger_moves_compaction_under_it():
    model = SimpleNamespace(profile=None, model_name="openai/small-model")

    kwargs = _built_with(model, catalog_window=128_000)

    assert kwargs["trigger"] == ("tokens", int(128_000 * COMPACTION_WINDOW_FRACTION))
    assert kwargs["truncate_args_settings"] is None


def test_a_catalog_window_above_the_fixed_trigger_keeps_it():
    model = SimpleNamespace(profile=None, model_name="anthropic/claude-sonnet-4.6")

    kwargs = _built_with(model, catalog_window=1_000_000)

    assert kwargs["trigger"] == compute_summarization_defaults(model)["trigger"]
    assert kwargs["truncate_args_settings"] is None


def test_an_unknown_window_keeps_deepagents_truncation():
    model = SimpleNamespace(profile=None, model_name="vendor/uncatalogued-model")

    kwargs = _built_with(model, catalog_window=None)

    defaults = compute_summarization_defaults(model)
    assert kwargs["trigger"] == defaults["trigger"]
    assert kwargs["truncate_args_settings"] == defaults["truncate_args_settings"]


@pytest.mark.parametrize("attribute", ["model_name", "model"])
def test_the_catalog_is_looked_up_by_the_configured_model_name(attribute):
    model = SimpleNamespace(profile=None, **{attribute: "z-ai/glm-5.1"})

    with (
        patch(f"{MODULE}.resolve_window_by_name", return_value=None) as lookup,
        patch(f"{MODULE}.SummarizationMiddleware"),
    ):
        build_summarization_middleware(model, StateBackend())

    lookup.assert_called_once_with("z-ai/glm-5.1")


def _screenshots(count: int) -> list:
    """A conversation of ``count`` small browser screenshots, each a tool call and its image result."""
    messages = [HumanMessage("Check the page", id="h0")]
    for i in range(count):
        call = {"id": f"t{i}", "name": "browser_screenshot", "args": {}}
        messages.append(AIMessage("", id=f"a{i}", tool_calls=[call]))
        image = {"type": "image", "base64": "iVBORw0KGgo=", "mime_type": "image/png"}
        messages.append(ToolMessage([image], tool_call_id=f"t{i}", id=f"tm{i}"))
    return messages


def _profiled_middleware(window: int):
    model = FakeListChatModel(responses=["ok"], profile={"max_input_tokens": window})
    return build_summarization_middleware(model, StateBackend())


@pytest.mark.parametrize("window", [200_000, 1_000_000])
def test_compaction_runs_once_the_conversation_holds_its_screenshot_budget(window):
    middleware = _profiled_middleware(window)
    below, at = _screenshots(MAX_IMAGES_BEFORE_COMPACTION - 10), _screenshots(MAX_IMAGES_BEFORE_COMPACTION)

    assert not middleware._should_summarize(below, middleware.token_counter(below))
    assert middleware._should_summarize(at, middleware.token_counter(at))


@pytest.mark.parametrize("window", [200_000, 1_000_000])
def test_what_compaction_keeps_holds_too_few_screenshots_to_compact_again(window):
    middleware = _profiled_middleware(window)
    messages = _screenshots(MAX_IMAGES_BEFORE_COMPACTION)

    kept = messages[middleware._determine_cutoff_index(messages) :]

    assert sum(isinstance(m, ToolMessage) for m in kept) < MAX_IMAGES_BEFORE_COMPACTION // 2


async def test_screenshots_below_the_budget_reach_the_model_unchanged():
    messages = _screenshots(MAX_IMAGES_BEFORE_COMPACTION - 10)
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

    await _profiled_middleware(1_000_000).awrap_model_call(request, handler)

    assert sent == messages
