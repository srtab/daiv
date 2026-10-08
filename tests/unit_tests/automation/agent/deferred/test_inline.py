import warnings
from types import SimpleNamespace

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.chat_models import _supports_mid_conversation_system_messages
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from automation.agent.chat_models import ChatOpenRouter
from automation.agent.deferred import inline as inline_module
from automation.agent.deferred.conf import DeferredToolsSettings
from automation.agent.deferred.index import DeferredToolsIndex
from automation.agent.deferred.inline import LOADED_TOOLS_ARTIFACT_KEY, inline_block_builder, with_inline_definitions


class _DigestArgs(BaseModel):
    ticket: str = Field(description="Ticket identifier.")
    note_window: int = Field(default=5, description="Maximum number of notes.")


def _tool(name: str, args_schema: type[BaseModel] = _DigestArgs) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda **kwargs: "ok", name=name, description=f"{name} description.", args_schema=args_schema
    )


def _index(*names: str) -> DeferredToolsIndex:
    return DeferredToolsIndex([_tool(name) for name in names])


def _search_call(call_id: str, names: list[str]) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": "tool_search", "id": call_id, "args": {"select": names}, "type": "tool_call"}]
    )


def _search_result(call_id: str, names: list[str], *, status: str = "success") -> ToolMessage:
    return ToolMessage(
        content=f"Loaded {len(names)} tool(s).",
        tool_call_id=call_id,
        status=status,
        artifact={LOADED_TOOLS_ARTIFACT_KEY: names},
    )


def _tool_call(call_id: str, name: str) -> AIMessage:
    call = {"name": name, "id": call_id, "args": {"ticket": "A-1"}, "type": "tool_call"}
    return AIMessage(content="", tool_calls=[call])


def _declared(message: SystemMessage) -> list[str]:
    return [block["tool"]["definition"]["name"] for block in message.content]


def _anthropic(model: str = "claude-opus-5-5") -> ChatAnthropic:
    return ChatAnthropic(model=model, api_key="sk-ant-not-used")


class TestInlineBlockBuilder:
    @pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-4-8"])
    def test_supported_anthropic_models_get_tool_additions(self, model):
        assert inline_block_builder(_anthropic(model)) is inline_module._anthropic_block

    def test_openai_responses_model_gets_additional_tools(self):
        model = ChatOpenAI(model="gpt-6-astra", api_key="sk-not-used", use_responses_api=True)
        assert inline_block_builder(model) is inline_module._openai_block

    @pytest.mark.parametrize(
        "model",
        [
            _anthropic("claude-sonnet-4-6"),
            ChatOpenAI(model="gpt-6-astra", api_key="sk-not-used"),
            ChatOpenRouter(model="anthropic/claude-opus-5-5", api_key="sk-not-used"),
            ChatOpenRouter(model="openai/gpt-5.6-luna", api_key="sk-not-used", use_responses_api=True),
            None,
        ],
        ids=["unlisted-claude", "openai-chat-completions", "openrouter-claude", "openrouter-gpt", "no-model"],
    )
    def test_other_models_get_nothing(self, model):
        assert inline_block_builder(model) is None

    def test_listed_anthropic_model_langchain_would_hoist_gets_nothing(self, monkeypatch):
        # langchain-anthropic raises on a non-leading system message for such a model, so listing it must not inline.
        monkeypatch.setattr(inline_module.deferred_settings, "INLINE_TOOLS_MODELS", ["claude-haiku-5-5"])
        assert inline_block_builder(_anthropic("claude-haiku-5-5")) is None

    def test_empty_setting_disables_it(self, monkeypatch):
        monkeypatch.setattr(inline_module.deferred_settings, "INLINE_TOOLS_MODELS", [])
        assert inline_block_builder(_anthropic()) is None

    def test_embed_valve_off_disables_it(self, monkeypatch):
        monkeypatch.setattr(inline_module.deferred_settings, "EMBED_SCHEMAS_IN_RESULTS", False)
        assert inline_block_builder(_anthropic()) is None

    def test_every_default_anthropic_prefix_is_kept_in_place_by_langchain(self):
        prefixes = [p for p in DeferredToolsSettings().INLINE_TOOLS_MODELS if p.startswith("claude-")]
        assert prefixes
        assert all(_supports_mid_conversation_system_messages(prefix) for prefix in prefixes)


class TestBlocks:
    def test_anthropic_block_carries_the_full_definition(self):
        entry = _index("rt_digest").get("rt_digest")
        block = inline_module._anthropic_block(entry)

        assert block["type"] == "tool_addition"
        assert block["tool"]["type"] == "tool_definition"
        definition = block["tool"]["definition"]
        assert definition["name"] == "rt_digest"
        assert set(definition["input_schema"]["properties"]) == {"ticket", "note_window"}
        assert "cache_control" not in definition

    def test_anthropic_block_skips_a_root_combinator_schema(self):
        tool = StructuredTool(
            name="rt_union",
            description="d",
            args_schema={"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}}}]},
            func=lambda **kwargs: "ok",
        )
        assert inline_module._anthropic_block(DeferredToolsIndex([tool]).get("rt_union")) is None

    @pytest.mark.parametrize("build", [inline_module._anthropic_block, inline_module._openai_block])
    def test_blocks_skip_a_tool_without_schema(self, build):
        assert build(SimpleNamespace(openai_schema=None, anthropic_definition=None)) is None

    def test_openai_block_is_a_responses_function(self):
        entry = _index("rt_digest").get("rt_digest")
        block = inline_module._openai_block(entry)

        assert block["type"] == "additional_tools"
        assert block["role"] == "developer"
        [function] = block["tools"]
        assert function["type"] == "function"
        assert function["name"] == "rt_digest"
        assert function["parameters"] == convert_to_openai_tool(entry.tool)["function"]["parameters"]


class TestWithInlineDefinitions:
    def _insert(self, messages, loaded, index=None):
        index = index or _index("rt_digest", "rt_lookup", "zz_other")
        return with_inline_definitions(messages, index, loaded, inline_module._anthropic_block)

    def test_declares_right_after_the_loading_result_when_it_is_last(self):
        messages = [HumanMessage("go"), _search_call("ts", ["rt_digest"]), _search_result("ts", ["rt_digest"])]

        result = self._insert(messages, {"rt_digest"})

        assert result[:3] == messages
        assert isinstance(result[3], SystemMessage)
        assert _declared(result[3]) == ["rt_digest"]

    def test_declares_before_the_next_assistant_turn(self):
        messages = [
            HumanMessage("go"),
            _search_call("ts", ["rt_digest"]),
            _search_result("ts", ["rt_digest"]),
            _tool_call("c1", "rt_digest"),
            ToolMessage("digest", tool_call_id="c1"),
        ]

        result = self._insert(messages, {"rt_digest"})

        assert [type(m) for m in result] == [
            HumanMessage, AIMessage, ToolMessage, SystemMessage, AIMessage, ToolMessage,
        ]  # fmt: skip

    def test_skips_past_the_rest_of_the_batch_and_appended_reminders(self):
        messages = [
            HumanMessage("go"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "tool_search", "id": "ts", "args": {"select": ["rt_digest"]}, "type": "tool_call"},
                    {"name": "read_file", "id": "rf", "args": {"file_path": "/a"}, "type": "tool_call"},
                ],
            ),
            _search_result("ts", ["rt_digest"]),
            ToolMessage("file", tool_call_id="rf"),
            HumanMessage("Step budget reminder."),
        ]

        result = self._insert(messages, {"rt_digest"})

        assert result[:5] == messages
        assert _declared(result[5]) == ["rt_digest"]

    def test_tools_loaded_together_share_one_message_sorted_by_name(self):
        messages = [HumanMessage("go"), _search_call("ts", ["zz_other", "rt_digest"])]
        messages.append(_search_result("ts", ["zz_other", "rt_digest"]))

        result = self._insert(messages, {"zz_other", "rt_digest"})

        assert len(result) == 4
        assert _declared(result[3]) == ["rt_digest", "zz_other"]

    def test_tools_loaded_apart_are_declared_at_their_own_results(self):
        messages = [
            HumanMessage("go"),
            _search_call("ts1", ["rt_digest"]),
            _search_result("ts1", ["rt_digest"]),
            _search_call("ts2", ["rt_lookup"]),
            _search_result("ts2", ["rt_lookup"]),
        ]

        result = self._insert(messages, {"rt_digest", "rt_lookup"})

        assert [type(m) for m in result] == [
            HumanMessage, AIMessage, ToolMessage, SystemMessage, AIMessage, ToolMessage, SystemMessage,
        ]  # fmt: skip
        assert _declared(result[3]) == ["rt_digest"]
        assert _declared(result[6]) == ["rt_lookup"]

    def test_a_reselected_tool_stays_at_its_first_result(self):
        messages = [
            HumanMessage("go"),
            _search_call("ts1", ["rt_digest"]),
            _search_result("ts1", ["rt_digest"]),
            _search_call("ts2", ["rt_digest"]),
            _search_result("ts2", ["rt_digest"]),
        ]

        result = self._insert(messages, {"rt_digest"})

        assert isinstance(result[3], SystemMessage)
        assert len(result) == len(messages) + 1

    def test_a_tool_without_a_visible_anchor_is_declared_after_the_first_human_turn(self):
        messages = [HumanMessage("summary of earlier work"), _tool_call("c1", "rt_digest")]

        result = self._insert(messages, {"rt_digest"})

        assert isinstance(result[0], HumanMessage)
        assert _declared(result[1]) == ["rt_digest"]
        assert result[2] is messages[1]

    def test_an_errored_result_is_not_an_anchor(self):
        messages = [
            HumanMessage("go"),
            _search_call("ts1", ["rt_digest"]),
            _search_result("ts1", ["rt_digest"], status="error"),
            _search_call("ts2", ["rt_digest"]),
            _search_result("ts2", ["rt_digest"]),
        ]

        result = self._insert(messages, {"rt_digest"})

        assert [type(m) for m in result] == [
            HumanMessage, AIMessage, ToolMessage, AIMessage, ToolMessage, SystemMessage,
        ]  # fmt: skip

    def test_unindexed_and_undeclarable_tools_add_nothing(self):
        messages = [HumanMessage("go"), _search_call("ts", ["gone"]), _search_result("ts", ["gone"])]

        assert self._insert(messages, {"gone"}) == messages
        assert with_inline_definitions(messages, _index("gone"), {"gone"}, lambda entry: None) == messages

    def test_rebuilding_on_a_longer_history_keeps_the_earlier_bytes(self):
        first = [HumanMessage("go"), _search_call("ts", ["rt_digest"]), _search_result("ts", ["rt_digest"])]
        later = [*first, _tool_call("c1", "rt_digest"), ToolMessage("digest", tool_call_id="c1")]

        sent_first = self._insert(first, {"rt_digest"})
        sent_later = self._insert(later, {"rt_digest"})

        assert sent_later[: len(sent_first)] == sent_first


class TestOnTheWire:
    """The declarations as langchain sends them: a mid-conversation turn, never hoisted into ``system``."""

    def _anthropic_messages(self, messages):
        model = _anthropic()
        prompt = [SystemMessage("You are DAIV."), *messages]
        sent = with_inline_definitions(prompt, _index("rt_digest"), {"rt_digest"}, inline_module._anthropic_block)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            return model._get_request_payload(sent)

    def test_anthropic_keeps_the_declaration_in_place(self):
        payload = self._anthropic_messages([
            HumanMessage("go"),
            _search_call("ts", ["rt_digest"]),
            _search_result("ts", ["rt_digest"]),
        ])

        assert payload["system"] == "You are DAIV."
        assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user", "system"]
        assert payload["messages"][-1]["content"][0]["type"] == "tool_addition"
        assert "inline-tools-2026-09-15" in payload["betas"]

    def test_anthropic_accepts_it_after_an_appended_reminder(self):
        payload = self._anthropic_messages([
            HumanMessage("go"),
            _search_call("ts", ["rt_digest"]),
            _search_result("ts", ["rt_digest"]),
            HumanMessage("Step budget reminder."),
        ])

        assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user", "system"]

    def test_anthropic_payload_extends_the_previous_one(self):
        first = [HumanMessage("go"), _search_call("ts", ["rt_digest"]), _search_result("ts", ["rt_digest"])]
        later = [*first, _tool_call("c1", "rt_digest"), ToolMessage("digest", tool_call_id="c1")]

        sent_first = self._anthropic_messages(first)["messages"]
        sent_later = self._anthropic_messages(later)["messages"]

        assert sent_later[: len(sent_first)] == sent_first

    def test_openai_responses_sends_an_additional_tools_item(self):
        model = ChatOpenAI(model="gpt-6-astra", api_key="sk-not-used", use_responses_api=True)
        messages = [HumanMessage("go"), _search_call("ts", ["rt_digest"]), _search_result("ts", ["rt_digest"])]
        sent = with_inline_definitions(messages, _index("rt_digest"), {"rt_digest"}, inline_module._openai_block)

        payload = model._get_request_payload(sent)

        assert payload["input"][-1]["type"] == "additional_tools"
        assert payload["input"][-1]["tools"][0]["name"] == "rt_digest"
