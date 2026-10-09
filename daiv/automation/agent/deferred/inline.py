"""Native mid-conversation tool definitions for loaded deferred tools.

On a model whose API takes a tool definition mid-conversation (``INLINE_TOOLS_MODELS``), each loaded deferred tool is
declared in a ``SystemMessage`` right after the batch of tool results holding the ``tool_search`` result that loaded
it: an Anthropic ``tool_addition`` block or an OpenAI Responses ``additional_tools`` item. The tools array keeps its
frozen shape and every earlier message keeps its bytes, so a load costs no cache miss, and the model calls a declared
tool with provider-typed arguments instead of reading the schema out of the result text.

The declarations live only in the request: every call rebuilds them from ``loaded_tool_names`` and the
``loaded_tools`` artifact ``tool_search`` leaves on its result, so each lands at the same position with the same bytes.
A loaded tool whose anchor is gone (compacted away, or loaded before anchors existed) is declared after the first
human turn instead, a one-off shift that the compaction already paid for. A tool that can't be declared (no JSON
schema, or a top-level ``oneOf``/``anyOf``/``allOf``, which Anthropic rejects) stays reachable the frozen way, through
the schema in its ``tool_search`` result.

Anthropic accepts a mid-conversation system turn only before an assistant turn or at the end, and langchain-anthropic
raises on one followed by a user turn. The declarations therefore have to be inserted after every reminder or nudge a
middleware appends: ``InlineToolDefinitionsMiddleware`` runs after every DAIV middleware, and the deepagents tail inside
it (skills, prompt caching, memory) appends no messages.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_anthropic import ChatAnthropic
from langchain_anthropic.chat_models import _supports_mid_conversation_system_messages
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from automation.agent.deferred.conf import settings as deferred_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from langchain_core.messages import AnyMessage

    from automation.agent.deferred.index import DeferredToolsIndex, ToolEntry

LOADED_TOOLS_ARTIFACT_KEY = "loaded_tools"

_ANTHROPIC_ROOT_COMBINATORS = ("oneOf", "anyOf", "allOf")


def inline_block_builder(model: object) -> Callable[[ToolEntry], dict[str, Any] | None] | None:
    """How ``model`` takes a tool definition mid-conversation, or ``None`` when it doesn't.

    Also ``None`` when ``EMBED_SCHEMAS_IN_RESULTS`` is off: that valve sends every model back to the array append.
    """
    if not deferred_settings.EMBED_SCHEMAS_IN_RESULTS:
        return None
    prefixes = tuple(deferred_settings.INLINE_TOOLS_MODELS)
    if (
        type(model) is ChatAnthropic
        and model.model.startswith(prefixes)
        and _supports_mid_conversation_system_messages(model.model)
    ):
        return _anthropic_block
    if type(model) is ChatOpenAI and model.use_responses_api is True and model.model_name.startswith(prefixes):
        return _openai_block
    return None


def _anthropic_block(entry: ToolEntry) -> dict[str, Any] | None:
    definition = entry.anthropic_definition
    if definition is None or any(key in definition["input_schema"] for key in _ANTHROPIC_ROOT_COMBINATORS):
        return None
    return {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": definition}}


def _openai_block(entry: ToolEntry) -> dict[str, Any] | None:
    if entry.openai_schema is None:
        return None
    function = {"type": "function", **entry.openai_schema["function"]}
    return {"type": "additional_tools", "role": "developer", "tools": [function]}


def _anchors(messages: Sequence[AnyMessage], loaded: Iterable[str]) -> dict[str, int]:
    """Each loaded tool's anchor: the first successful result that loaded it, else the first human turn."""
    loaded_at: dict[str, int] = {}
    for position, message in enumerate(messages):
        if isinstance(message, ToolMessage) and message.status != "error" and isinstance(message.artifact, dict):
            for name in message.artifact.get(LOADED_TOOLS_ARTIFACT_KEY) or ():
                loaded_at.setdefault(name, position)
    first_human = next((i for i, message in enumerate(messages) if isinstance(message, HumanMessage)), None)
    anchors = {name: loaded_at.get(name, first_human) for name in loaded}
    return {name: anchor for name, anchor in anchors.items() if anchor is not None}


def _insertion_point(messages: Sequence[AnyMessage], anchor: int) -> int:
    """The position after the anchor's batch of user-side messages, which is where a system turn may go.

    An empty reply is skipped too: Anthropic drops it, which would leave no assistant turn after the declaration.
    """
    position = anchor + 1
    while position < len(messages) and (
        isinstance(message := messages[position], ToolMessage | HumanMessage)
        or (isinstance(message, AIMessage) and not message.content and not message.tool_calls)
    ):
        position += 1
    return position


def with_inline_definitions(
    messages: Sequence[AnyMessage],
    index: DeferredToolsIndex,
    loaded: Iterable[str],
    build: Callable[[ToolEntry], dict[str, Any] | None],
) -> list[AnyMessage]:
    """``messages`` with each loaded tool ``build`` can declare, in one ``SystemMessage`` per insertion point."""
    blocks_at: dict[int, list[str | dict]] = {}
    for name, anchor in sorted(_anchors(messages, loaded).items()):
        entry = index.get(name)
        block = build(entry) if entry is not None else None
        if block is not None:
            blocks_at.setdefault(_insertion_point(messages, anchor), []).append(block)

    result = list(messages)
    for position in sorted(blocks_at, reverse=True):
        result.insert(position, SystemMessage(content=blocks_at[position]))
    return result
