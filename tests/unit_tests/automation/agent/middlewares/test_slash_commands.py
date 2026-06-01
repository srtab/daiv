from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

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


async def test_no_slash_command_returns_none():
    mw = SlashCommandMiddleware(subagents=[])
    state = {"messages": [HumanMessage(content="just a question")]}
    assert await mw.abefore_agent(state, _runtime(), {}) is None


async def test_executes_builtin_command_and_jumps_to_end():
    mw = SlashCommandMiddleware(subagents=[])
    command = MagicMock()
    command.resets_thread = False
    command.execute_for_agent = AsyncMock(return_value="help text")
    command_cls = MagicMock(return_value=command)

    state = {"messages": [HumanMessage(content="/help")]}
    with (
        patch.object(
            mw, "_extract_slash_command", return_value=SlashCommandCommand(raw="/help", command="help", args=[])
        ),
        patch("automation.agent.middlewares.slash_commands.slash_command_registry") as registry,
        patch(
            "automation.agent.middlewares.slash_commands._load_global_skill_metadata",
            return_value=[{"name": "x", "description": "d"}],
        ),
    ):
        registry.get_commands.return_value = [command_cls]
        result = await mw.abefore_agent(state, _runtime(), {})

    assert result["jump_to"] == "end"
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "help text"
    # A non-resetting command must NOT touch active_skill_mode (SkillsMiddleware clears it on follow-up).
    assert "active_skill_mode" not in result
    # /help got the disk-loaded global skills
    assert command.execute_for_agent.await_args.kwargs["available_skills"] == [{"name": "x", "description": "d"}]
    assert command.execute_for_agent.await_args.kwargs["available_subagents"] == []


async def test_resets_thread_prepends_remove_all():
    mw = SlashCommandMiddleware(subagents=[])
    command = MagicMock()
    command.resets_thread = True
    command.execute_for_agent = AsyncMock(return_value="cleared")
    command_cls = MagicMock(return_value=command)

    state = {"messages": [HumanMessage(content="/clear")]}
    with (
        patch.object(
            mw, "_extract_slash_command", return_value=SlashCommandCommand(raw="/clear", command="clear", args=[])
        ),
        patch("automation.agent.middlewares.slash_commands.slash_command_registry") as registry,
        patch("automation.agent.middlewares.slash_commands._load_global_skill_metadata", return_value=[]),
    ):
        registry.get_commands.return_value = [command_cls]
        result = await mw.abefore_agent(state, _runtime(scope=Scope.ISSUE), {})

    assert result["jump_to"] == "end"
    assert getattr(result["messages"][0], "id", None) == REMOVE_ALL_MESSAGES
    # A thread reset must also clear active_skill_mode, else a read-only skill stays stuck on the
    # fresh thread (history is wiped, so SkillsMiddleware's clear-on-followup can never fire).
    assert result["active_skill_mode"] is None


async def test_command_failure_jumps_to_end_with_error_message():
    mw = SlashCommandMiddleware(subagents=[])
    command = MagicMock()
    command.execute_for_agent = AsyncMock(side_effect=RuntimeError("boom"))
    command_cls = MagicMock(return_value=command)

    state = {"messages": [HumanMessage(content="/help")]}
    with (
        patch.object(
            mw, "_extract_slash_command", return_value=SlashCommandCommand(raw="/help", command="help", args=[])
        ),
        patch("automation.agent.middlewares.slash_commands.slash_command_registry") as registry,
        patch("automation.agent.middlewares.slash_commands._load_global_skill_metadata", return_value=[]),
    ):
        registry.get_commands.return_value = [command_cls]
        result = await mw.abefore_agent(state, _runtime(), {})

    assert result["jump_to"] == "end"
    assert "Failed to execute `/help`." in result["messages"][-1].content


async def test_unknown_command_falls_through_without_jump():
    """An unregistered command must NOT short-circuit — it falls through so the agent handles it."""
    mw = SlashCommandMiddleware(subagents=[])
    command = MagicMock()
    command.execute_for_agent = AsyncMock()
    state = {"messages": [HumanMessage(content="/nope")]}
    with (
        patch.object(
            mw, "_extract_slash_command", return_value=SlashCommandCommand(raw="/nope", command="nope", args=[])
        ),
        patch("automation.agent.middlewares.slash_commands.slash_command_registry") as registry,
    ):
        registry.get_commands.return_value = []
        result = await mw.abefore_agent(state, _runtime(), {})

    assert result is None
    command.execute_for_agent.assert_not_awaited()


async def test_ambiguous_command_falls_through_without_executing():
    """More than one command for the same name is ambiguous — fall through, do not execute either."""
    mw = SlashCommandMiddleware(subagents=[])
    command = MagicMock()
    command.execute_for_agent = AsyncMock()
    state = {"messages": [HumanMessage(content="/demo")]}
    with (
        patch.object(
            mw, "_extract_slash_command", return_value=SlashCommandCommand(raw="/demo", command="demo", args=[])
        ),
        patch("automation.agent.middlewares.slash_commands.slash_command_registry") as registry,
    ):
        registry.get_commands.return_value = [MagicMock(command="demo"), MagicMock(command="demo")]
        result = await mw.abefore_agent(state, _runtime(), {})

    assert result is None
    command.execute_for_agent.assert_not_awaited()


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
