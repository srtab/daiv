from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from automation.agent.constants import ASSISTANT_MESSAGE_EVENT
from automation.agent.middlewares.slash_commands import SlashCommandMiddleware, _load_global_skill_metadata
from codebase.base import Scope
from slash_commands.parser import SlashCommandCommand

if TYPE_CHECKING:
    from pathlib import Path


def _write_skill(root: Path, name: str, description: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n", encoding="utf-8")


def test_load_global_skill_metadata_reads_builtin_and_custom(tmp_path: Path):
    builtin = tmp_path / "builtin"
    custom = tmp_path / "custom"
    _write_skill(builtin, "code-review", "Review a diff")
    _write_skill(custom, "deploy", "Deploy the app")

    with (
        patch("automation.agent.middlewares.slash_commands.BUILTIN_SKILLS_PATH", builtin),
        patch("automation.agent.middlewares.slash_commands.agent_settings") as settings,
    ):
        settings.CUSTOM_SKILLS_PATH = custom
        skills = _load_global_skill_metadata()

    by_name = {s["name"]: s["description"] for s in skills}
    assert by_name["code-review"] == "Review a diff"
    assert by_name["deploy"] == "Deploy the app"


def test_load_global_skill_metadata_custom_overrides_builtin(tmp_path: Path):
    builtin = tmp_path / "builtin"
    custom = tmp_path / "custom"
    _write_skill(builtin, "shared", "builtin version")
    _write_skill(custom, "shared", "custom version")

    with (
        patch("automation.agent.middlewares.slash_commands.BUILTIN_SKILLS_PATH", builtin),
        patch("automation.agent.middlewares.slash_commands.agent_settings") as settings,
    ):
        settings.CUSTOM_SKILLS_PATH = custom
        skills = _load_global_skill_metadata()

    by_name = {s["name"]: s["description"] for s in skills}
    assert by_name["shared"] == "custom version"


def test_load_global_skill_metadata_skips_missing_custom_dir(tmp_path: Path):
    builtin = tmp_path / "builtin"
    _write_skill(builtin, "only-builtin", "x")

    with (
        patch("automation.agent.middlewares.slash_commands.BUILTIN_SKILLS_PATH", builtin),
        patch("automation.agent.middlewares.slash_commands.agent_settings") as settings,
    ):
        settings.CUSTOM_SKILLS_PATH = tmp_path / "does-not-exist"
        skills = _load_global_skill_metadata()

    assert [s["name"] for s in skills] == ["only-builtin"]


def _runtime(*, scope=Scope.GLOBAL, bot_username="daiv"):
    rt = MagicMock()
    rt.context.scope = scope
    rt.context.bot_username = bot_username
    rt.context.repository.slug = "group/repo"
    rt.context.issue = None
    rt.context.merge_request = None
    return rt


def _command_stub(*, resets_thread=False, **execute_kwargs):
    """A registered command whose ``execute_for_agent`` behaves as the mock kwargs describe."""
    command = MagicMock()
    command.resets_thread = resets_thread
    command.execute_for_agent = AsyncMock(**execute_kwargs)
    return command


async def _run_command(*, raw, commands, skills=(), scope=Scope.GLOBAL, emit=None):
    """Drive ``abefore_agent`` past the parser with the registry, skill list and event sink stubbed.

    Returns ``(result, emit)``; the command stubs stay addressable through the ``commands`` passed in.
    """
    mw = SlashCommandMiddleware(subagents=[])
    emit = emit if emit is not None else AsyncMock()
    parsed = SlashCommandCommand(raw=raw, command=raw.lstrip("/"), args=[])
    with (
        patch.object(mw, "_extract_slash_command", return_value=parsed),
        patch("automation.agent.middlewares.slash_commands.slash_command_registry") as registry,
        patch("automation.agent.middlewares.slash_commands._load_global_skill_metadata", return_value=list(skills)),
        patch("automation.agent.utils.adispatch_custom_event", emit),
    ):
        registry.get_commands.return_value = [MagicMock(return_value=c) for c in commands]
        result = await mw.abefore_agent({"messages": [HumanMessage(content=raw)]}, _runtime(scope=scope), {"cfg": 1})
    return result, emit


async def test_no_slash_command_returns_none():
    mw = SlashCommandMiddleware(subagents=[])
    state = {"messages": [HumanMessage(content="just a question")]}
    assert await mw.abefore_agent(state, _runtime(), {}) is None


async def test_executes_builtin_command_and_jumps_to_end():
    command = _command_stub(return_value="help text")
    result, _ = await _run_command(raw="/help", commands=[command], skills=[{"name": "x", "description": "d"}])

    assert result["jump_to"] == "end"
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "help text"
    # A non-resetting command must NOT touch active_skill_mode (SkillsMiddleware clears it on follow-up).
    assert "active_skill_mode" not in result
    # /help got the disk-loaded global skills
    assert command.execute_for_agent.await_args.kwargs["available_skills"] == [{"name": "x", "description": "d"}]
    assert command.execute_for_agent.await_args.kwargs["available_subagents"] == []


async def test_resets_thread_prepends_remove_all():
    command = _command_stub(return_value="cleared", resets_thread=True)
    result, _ = await _run_command(raw="/clear", commands=[command], scope=Scope.ISSUE)

    assert result["jump_to"] == "end"
    assert getattr(result["messages"][0], "id", None) == REMOVE_ALL_MESSAGES
    # A thread reset must also clear active_skill_mode, else a read-only skill stays stuck on the
    # fresh thread (history is wiped, so SkillsMiddleware's clear-on-followup can never fire).
    assert result["active_skill_mode"] is None


async def test_command_failure_jumps_to_end_with_error_message():
    command = _command_stub(side_effect=RuntimeError("boom"))
    result, emit = await _run_command(raw="/help", commands=[command])

    assert result["jump_to"] == "end"
    assert "Failed to execute `/help`." in result["messages"][-1].content
    # The failure reply streams like any other, else the turn reports the error only on reload.
    assert emit.await_args.args[1]["message"] == "Failed to execute `/help`."


async def test_unknown_command_falls_through_without_jump():
    """An unregistered command must NOT short-circuit — it falls through so the agent handles it."""
    result, emit = await _run_command(raw="/nope", commands=[])

    assert result is None
    emit.assert_not_awaited()


async def test_ambiguous_command_falls_through_without_executing():
    """More than one command for the same name is ambiguous — fall through, do not execute either."""
    first, second = _command_stub(), _command_stub()
    result, _ = await _run_command(raw="/demo", commands=[first, second])

    assert result is None
    first.execute_for_agent.assert_not_awaited()
    second.execute_for_agent.assert_not_awaited()


async def test_reply_is_streamed_so_the_chat_turn_is_not_empty():
    """A slash command answers from a state update, never from the model, so nothing synthesizes
    TEXT_MESSAGE_* frames for it — without this event the chat turn paints empty."""
    command = _command_stub(return_value="### Available Sub-Agents")
    result, emit = await _run_command(raw="/agents", commands=[command])

    name, payload = emit.await_args.args
    assert name == ASSISTANT_MESSAGE_EVENT
    assert payload["message"] == "### Available Sub-Agents"
    # The hook is handed a config; pass it rather than resolving the ambient one.
    assert emit.await_args.kwargs["config"] == {"cfg": 1}
    assert result["messages"][-1].id == payload["message_id"]


def test_extract_slash_command_requires_human_message():
    mw = SlashCommandMiddleware(subagents=[])
    assert mw._extract_slash_command([AIMessage(content="hello")], "daiv") is None


def test_extract_slash_command_skips_blank_content():
    mw = SlashCommandMiddleware(subagents=[])
    assert mw._extract_slash_command([HumanMessage(content="  \n\t ")], "daiv") is None


def test_extract_slash_command_parses_multimodal_content():
    mw = SlashCommandMiddleware(subagents=[])
    messages = [
        HumanMessage(
            content=[
                {"type": "text", "text": "@daiv /help arg1"},
                {"type": "image_url", "image_url": {"url": "https://example.com/demo.png"}},
            ]
        )
    ]
    result = mw._extract_slash_command(messages, "daiv")
    assert result is not None
    assert result.command == "help"
    assert result.args == ["arg1"]
    assert result.raw == "@daiv /help arg1"
