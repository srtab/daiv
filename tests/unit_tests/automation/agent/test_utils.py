from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage

from automation.agent.events import ASSISTANT_MESSAGE_EVENT
from automation.agent.utils import (
    FINAL_ASSISTANT_TEXT_MAX_CHARS,
    build_langsmith_config,
    conversation_thread_id,
    extract_text_content,
    final_assistant_text,
    repo_relative_text,
    streamed_assistant_message,
)


class TestConversationThreadId:
    def test_returns_none_outside_runnable_context(self):
        assert conversation_thread_id() is None

    def test_reads_configurable(self):
        with patch("automation.agent.utils.get_config", return_value={"configurable": {"thread_id": "t-42"}}):
            assert conversation_thread_id() == "t-42"


# Tests for extract_text_content


def test_extract_text_content_from_string():
    """Test extracting text from string content."""
    content = "This is a simple text message"
    result = extract_text_content(content)
    assert result == "This is a simple text message"
    assert isinstance(result, str)


def test_extract_text_content_from_empty_string():
    """Test extracting text from empty string."""
    content = ""
    result = extract_text_content(content)
    assert result == ""
    assert isinstance(result, str)


def test_extract_text_content_from_list_with_text_blocks():
    """Test extracting text from list of content blocks with type field."""
    content = [{"type": "text", "text": "Hello "}, {"type": "text", "text": "world!"}]
    result = extract_text_content(content)
    assert result == "Hello world!"
    assert isinstance(result, str)


def test_extract_text_content_from_list_with_mixed_blocks():
    """Test extracting text from list with mixed block types."""
    content = [
        {"type": "text", "text": "Text part"},
        {"type": "image", "url": "http://example.com/image.png"},
        {"type": "text", "text": " continues here"},
    ]
    result = extract_text_content(content)
    assert result == "Text part continues here"
    assert isinstance(result, str)


def test_extract_text_content_from_list_with_text_field_only():
    """Test extracting text from list of blocks with only text field."""
    content = [{"text": "First part"}, {"text": " second part"}]
    result = extract_text_content(content)
    assert result == "First part second part"
    assert isinstance(result, str)


def test_extract_text_content_from_list_with_string_items():
    """Test extracting text from list of plain strings."""
    content = ["Hello ", "world", "!"]
    result = extract_text_content(content)
    assert result == "Hello world!"
    assert isinstance(result, str)


def test_extract_text_content_from_empty_list():
    """Test extracting text from empty list."""
    content = []
    result = extract_text_content(content)
    assert result == ""
    assert isinstance(result, str)


def test_extract_text_content_from_list_with_no_text():
    """Test extracting text from list with no text content."""
    content = [
        {"type": "image", "url": "http://example.com/image.png"},
        {"type": "audio", "url": "http://example.com/audio.mp3"},
    ]
    result = extract_text_content(content)
    assert result == ""
    assert isinstance(result, str)


def test_extract_text_content_from_complex_structure():
    """Test extracting text from complex multimodal content structure."""
    # This simulates the structure that was causing the bug
    content = [
        {"id": "rs_050509c9a1c76d8900690cbf77cf5481a0aed3d5a2228f6dd0", "type": "text", "text": "Sure, "},
        {"type": "text", "text": "I can help with that."},
    ]
    result = extract_text_content(content)
    assert result == "Sure, I can help with that."
    assert isinstance(result, str)


def test_extract_text_content_handles_unexpected_types_gracefully():
    """Test that unexpected content types are converted to string."""
    content = 123
    result = extract_text_content(content)
    assert result == "123"
    assert isinstance(result, str)


def test_extract_text_content_from_none():
    """Test extracting text from None value."""
    content = None
    result = extract_text_content(content)
    assert result == "None"
    assert isinstance(result, str)


class TestFinalAssistantText:
    def test_returns_the_closing_summary(self):
        messages = [HumanMessage("fix the parser"), AIMessage("Fixed it; the suite was not run.")]

        assert final_assistant_text(messages) == "Fixed it; the suite was not run."

    def test_walks_back_past_a_tool_call_only_message(self):
        """A crashed or tool-terminated turn ends on an AIMessage carrying only tool_calls; the last
        prose the agent wrote is the summary, so the walk must not stop at the empty one."""
        messages = [
            AIMessage("Done, but I could not install dependencies."),
            AIMessage("", tool_calls=[{"name": "bash", "args": {}, "id": "c1"}]),
        ]

        assert final_assistant_text(messages) == "Done, but I could not install dependencies."

    def test_skips_a_loop_breaker_message_addressed_to_a_parent_agent(self):
        """LoopBreakerMiddleware's terminal="error" message is parsed by the code-review
        orchestrator, not read by a human — putting it in an MR description leaks internals."""
        messages = [AIMessage("Refactored the client."), AIMessage("ERROR: exceeded the step budget")]

        assert final_assistant_text(messages) == "Refactored the client."

    def test_returns_none_when_the_agent_wrote_no_prose(self):
        messages = [HumanMessage("go"), AIMessage("", tool_calls=[{"name": "bash", "args": {}, "id": "c1"}])]

        assert final_assistant_text(messages) is None

    def test_returns_none_for_an_empty_transcript(self):
        assert final_assistant_text([]) is None

    def test_ignores_a_trailing_human_message(self):
        """Managers append the user's follow-up comment before re-invoking; the human's own text is
        never the agent's summary."""
        messages = [AIMessage("Added the endpoint."), HumanMessage("also add tests")]

        assert final_assistant_text(messages) == "Added the endpoint."

    def test_a_content_block_with_a_null_text_does_not_raise(self):
        """Providers send `text: null` on some blocks, and joining a None raised — which, on the
        publish path, aborted the whole publish rather than losing one sentence."""
        messages = [AIMessage(content=[{"type": "text", "text": None}, {"type": "text", "text": "Added it."}])]

        assert final_assistant_text(messages) == "Added it."

    def test_truncates_a_long_summary_and_marks_it(self):
        messages = [AIMessage("x" * (FINAL_ASSISTANT_TEXT_MAX_CHARS + 500))]

        result = final_assistant_text(messages)

        assert result is not None
        assert len(result) < FINAL_ASSISTANT_TEXT_MAX_CHARS + 100
        assert result.startswith("x" * 100)
        assert result.endswith("…[truncated]")


def _langsmith_ctx():
    return Mock(repository=Mock(slug="owner/repo"), git_platform=Mock(value="gitlab"), scope=None)


def test_build_langsmith_config_puts_agent_name_in_metadata():
    """agent_name must land in metadata as lc_agent_name, otherwise the passed metadata dict
    replaces the agent's bound lc_agent_name and the tool-call logger reads <unknown-agent>."""
    config = build_langsmith_config(_langsmith_ctx(), trigger="mention", model="m", agent_name="DAIV Agent")

    assert config["metadata"]["lc_agent_name"] == "DAIV Agent"
    # Still mirrored in tags for LangSmith filtering.
    assert "DAIV Agent" in config["tags"]


def test_build_langsmith_config_omits_agent_name_when_not_provided():
    """Without agent_name there is nothing to propagate; the key must stay absent."""
    config = build_langsmith_config(_langsmith_ctx(), trigger="mention", model="m")

    assert "lc_agent_name" not in config["metadata"]


async def test_streamed_assistant_message_emits_under_the_returned_message_id(emit_custom_event):
    """A message produced without a model call streams only because of this event — chat/api/
    streaming.py turns it into the TEXT_MESSAGE_* frames the client renders. The frame and the
    message share an id so the client's replay dedup skips it after a mid-run reload."""
    message = await streamed_assistant_message("stopped looping", {"cfg": 1})

    name, payload = emit_custom_event.await_args.args
    assert name == ASSISTANT_MESSAGE_EVENT
    assert payload == {"message_id": message.id, "message": "stopped looping"}
    assert emit_custom_event.await_args.kwargs["config"] == {"cfg": 1}
    assert message.content == "stopped looping"


async def test_streamed_assistant_message_defaults_to_the_ambient_config(emit_custom_event):
    """LoopBreaker's awrap_model_call is handed no RunnableConfig; the dispatch resolves the
    ambient one instead."""
    await streamed_assistant_message("stopped looping")

    assert emit_custom_event.await_args.kwargs["config"] is None


async def test_streamed_assistant_message_survives_an_undeliverable_event(emit_custom_event):
    """Streaming is cosmetic — the message still reaches the checkpoint, so it surfaces on
    reload. A failed frame must never fail the work that produced the message."""
    emit_custom_event.side_effect = RuntimeError("no parent run id")
    message = await streamed_assistant_message("stopped looping")

    assert message.content == "stopped looping"


class TestRepoRelativeText:
    def test_the_prefix_is_removed_wherever_a_path_starts(self):
        text = "Fixed /workspace/repo/daiv/x.py:3, see `/workspace/repo/daiv/y.py` and (/workspace/repo/z.md)."
        assert repo_relative_text(text) == "Fixed daiv/x.py:3, see `daiv/y.py` and (z.md)."

    def test_a_markdown_link_target_becomes_relative(self):
        assert repo_relative_text("[x](/workspace/repo/daiv/x.py#L3)") == "[x](daiv/x.py#L3)"

    def test_a_url_containing_the_prefix_is_left_alone(self):
        url = "https://gitlab.com/workspace/repo/-/blob/main/daiv/x.py"
        assert repo_relative_text(f"See {url}") == f"See {url}"

    def test_a_longer_path_ending_in_the_prefix_is_left_alone(self):
        assert repo_relative_text("/home/u/workspace/repo/x.py") == "/home/u/workspace/repo/x.py"

    def test_the_bare_root_is_left_alone(self):
        assert repo_relative_text("Cloned into /workspace/repo.") == "Cloned into /workspace/repo."

    def test_the_root_with_a_trailing_slash_is_left_alone(self):
        assert repo_relative_text("Cloned into `/workspace/repo/`.") == "Cloned into `/workspace/repo/`."
        assert repo_relative_text("Ran pytest in /workspace/repo/ and it passed") == (
            "Ran pytest in /workspace/repo/ and it passed"
        )
        assert repo_relative_text("cd /workspace/repo/ && make test") == "cd /workspace/repo/ && make test"
        assert repo_relative_text("Moved into /workspace/repo/.") == "Moved into /workspace/repo/."
        assert repo_relative_text("The root is /workspace/repo/") == "The root is /workspace/repo/"
        assert repo_relative_text("/workspace/repo//a.py") == "/workspace/repo//a.py"
        assert repo_relative_text("/workspace/repo/.agents/x and /workspace/repo/./x") == ".agents/x and ./x"
