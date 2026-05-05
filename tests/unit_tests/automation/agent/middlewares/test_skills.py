from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from automation.agent.constants import AGENTS_SKILLS_PATH, CLAUDE_CODE_SKILLS_PATH, CURSOR_SKILLS_PATH, SKILLS_SOURCES
from automation.agent.middlewares.skills import SkillsMiddleware
from codebase.base import Scope
from codebase.repo_config import RepositoryConfig, SlashCommands
from slash_commands.base import SlashCommand
from slash_commands.registry import SlashCommandRegistry


def _make_runtime(
    *, repo_working_dir: str, bot_username: str = "daiv-bot", scope: Scope = Scope.GLOBAL, repo_id: str = "repo-1"
) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.gitrepo = Mock(working_dir=repo_working_dir)
    runtime.context.bot_username = bot_username
    runtime.context.scope = scope
    runtime.context.repository = Mock(slug=repo_id)
    runtime.context.issue = None
    runtime.context.merge_request = None
    return runtime


def _make_skill_md(*, name: str, description: str, metadata: dict[str, str] | None = None) -> str:
    frontmatter_lines = ["---", f"name: {name}", f"description: {description}"]
    if metadata:
        frontmatter_lines.append("metadata:")
        for key, value in metadata.items():
            frontmatter_lines.append(f"  {key}: {value}")
    frontmatter_lines.append("---")
    frontmatter = "\n".join(frontmatter_lines)
    return f"{frontmatter}\n\n# {name}\n"


class TestSkillsMiddleware:
    """
    Test the SkillsMiddleware class.
    """

    async def test_copies_builtin_skills_then_delegates_to_super(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one" / "helpers").mkdir(parents=True)
        (builtin / "skill-two").mkdir(parents=True)
        (builtin / "__pycache__").mkdir(parents=True)
        (builtin / "not_a_dir.txt").write_text("ignore\n")

        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="does one"))
        (builtin / "skill-one" / "helpers" / "util.py").write_text("print('one')\n")
        (builtin / "skill-two" / "SKILL.md").write_text(_make_skill_md(name="skill-two", description="does two"))
        (builtin / "__pycache__" / "ignored.txt").write_text("ignored\n")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])
        runtime = _make_runtime(repo_working_dir=str(tmp_path / repo_name))

        with patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin):
            result = await middleware.abefore_agent({"messages": [HumanMessage(content="hello")]}, runtime, Mock())

        assert result is not None
        skills = {skill["name"]: skill for skill in result["skills_metadata"]}
        assert set(skills) == {"skill-one", "skill-two"}
        assert skills["skill-one"]["description"] == "does one"
        assert skills["skill-two"]["description"] == "does two"
        assert skills["skill-one"]["path"] == "/skills/skill-one/SKILL.md"
        assert skills["skill-two"]["path"] == "/skills/skill-two/SKILL.md"
        assert skills["skill-one"]["metadata"]["is_builtin"] is True
        assert skills["skill-two"]["metadata"]["is_builtin"] is True

    async def test_marks_builtin_metadata_and_clears_custom(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-two").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="does one"))
        (builtin / "skill-two" / "SKILL.md").write_text(_make_skill_md(name="skill-two", description="does two"))

        custom_skill = tmp_path / repo_name / AGENTS_SKILLS_PATH / "custom-skill"
        custom_skill.mkdir(parents=True)
        (custom_skill / "SKILL.md").write_text(
            _make_skill_md(
                name="custom-skill", description="does custom", metadata={"is_builtin": "true", "owner": "user"}
            )
        )

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills", f"/{repo_name}/.agents/skills"])
        runtime = _make_runtime(repo_working_dir=str(tmp_path / repo_name))

        with patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin):
            result = await middleware.abefore_agent({"messages": [HumanMessage(content="hello")]}, runtime, Mock())

        assert result is not None
        skills = {skill["name"]: skill for skill in result["skills_metadata"]}
        assert skills["skill-one"]["metadata"]["is_builtin"] is True
        assert skills["skill-two"]["metadata"]["is_builtin"] is True
        assert skills["custom-skill"]["metadata"]["owner"] == "user"
        assert "is_builtin" not in skills["custom-skill"]["metadata"]

    async def test_materializes_global_skills_under_skills_dir(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one" / "helpers").mkdir(parents=True)
        (builtin / "skill-two").mkdir(parents=True)
        (builtin / "__pycache__").mkdir(parents=True)
        (builtin / "not_a_dir.txt").write_text("ignore\n")

        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="does one"))
        (builtin / "skill-one" / "helpers" / "util.py").write_text("print('one')\n")
        (builtin / "skill-two" / "SKILL.md").write_text(_make_skill_md(name="skill-two", description="does two"))
        (builtin / "__pycache__" / "ignored.txt").write_text("ignored\n")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])

        with patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin):
            await middleware._copy_global_skills(agent_path=tmp_path / repo_name)

        skills_root = tmp_path / "skills"
        assert (skills_root / "skill-one" / "SKILL.md").read_text() == _make_skill_md(
            name="skill-one", description="does one"
        )
        assert (skills_root / "skill-one" / "helpers" / "util.py").read_text() == "print('one')\n"
        assert (skills_root / "skill-two" / "SKILL.md").read_text() == _make_skill_md(
            name="skill-two", description="does two"
        )
        assert not list(skills_root.rglob(".gitignore"))
        assert not (skills_root / "__pycache__").exists()
        assert not any(p.name == "not_a_dir.txt" for p in skills_root.rglob("*"))

    async def test_skips_file_upload_when_dest_exists(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one" / "helpers").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="builtin"))
        (builtin / "skill-one" / "helpers" / "util.py").write_text("print('one')\n")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])

        existing_skill_md = tmp_path / "skills" / "skill-one" / "SKILL.md"
        existing_skill_md.parent.mkdir(parents=True, exist_ok=True)
        existing_skill_md.write_text(_make_skill_md(name="skill-one", description="existing"))

        original_exists = Path.exists

        def fake_exists(self: Path) -> bool:
            # Map virtual `/skills/...` paths used during upload planning to the on-disk mirror.
            if str(self).startswith("/skills/"):
                mapped = tmp_path / str(self).lstrip("/")
                return original_exists(mapped)
            return original_exists(self)

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            patch("pathlib.Path.exists", new=fake_exists),
        ):
            await middleware._copy_global_skills(agent_path=tmp_path / repo_name)

        # SKILL.md must not be overwritten; sibling files are still uploaded.
        assert existing_skill_md.read_text() == _make_skill_md(name="skill-one", description="existing")
        assert (tmp_path / "skills" / "skill-one" / "helpers" / "util.py").read_text() == "print('one')\n"

    async def test_raises_when_backend_returns_error(self, tmp_path: Path):
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="does one"))

        backend = Mock()
        backend.aupload_files = AsyncMock(return_value=[Mock(error="boom")])
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            pytest.raises(RuntimeError, match="Failed to upload skill: boom"),
        ):
            await middleware._copy_global_skills(agent_path=tmp_path / "repoX")

    def test_format_skills_list_marks_builtin_and_global(self):
        middleware = SkillsMiddleware(backend=Mock(), sources=["/skills"])
        formatted = middleware._format_skills_list([
            {
                "name": "skill-one",
                "description": "does one",
                "path": "/skills/skill-one/SKILL.md",
                "metadata": {"is_builtin": True},
            },
            {
                "name": "global-skill",
                "description": "does global",
                "path": "/skills/global-skill/SKILL.md",
                "metadata": {"is_global": True},
            },
            {
                "name": "custom-skill",
                "description": "does custom",
                "path": "/skills/custom-skill/SKILL.md",
                "metadata": {},
            },
        ])

        assert formatted.startswith("<available_skills>")
        assert "<name>skill-one</name>" in formatted
        assert "<name>global-skill</name>" in formatted
        assert "<name>custom-skill</name>" in formatted
        assert formatted.count("<builtin>true</builtin>") == 1
        assert formatted.count("<global>true</global>") == 1

    def test_format_skills_list_returns_empty_hint(self):
        middleware = SkillsMiddleware(backend=Mock(), sources=["/skills", "/extra/skills"])
        formatted = middleware._format_skills_list([])
        assert formatted == "(No skills available yet. You can create skills in /skills or /extra/skills)"

    def test_extract_slash_command_requires_human_message(self):
        middleware = SkillsMiddleware(backend=Mock(), sources=["/skills"])
        messages = [AIMessage(content="hello")]
        assert middleware._extract_slash_command(messages, "daiv") is None

    def test_extract_slash_command_skips_blank_content(self):
        middleware = SkillsMiddleware(backend=Mock(), sources=["/skills"])
        messages = [HumanMessage(content="  \n\t ")]
        assert middleware._extract_slash_command(messages, "daiv") is None

    def test_extract_slash_command_parses_multimodal_content(self):
        middleware = SkillsMiddleware(backend=Mock(), sources=["/skills"])
        messages = [
            HumanMessage(
                content=[
                    {"type": "text", "text": "@daiv /help arg1"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/demo.png"}},
                ]
            )
        ]
        result = middleware._extract_slash_command(messages, "daiv")
        assert result is not None
        assert result.command == "help"
        assert result.args == ["arg1"]
        assert result.raw == "@daiv /help arg1"

    async def test_apply_builtin_slash_commands_executes_command(self):
        class DemoSlashCommand(SlashCommand):
            description = "demo"

            async def execute_for_agent(
                self,
                *,
                args: str,
                issue_iid: int | None = None,
                merge_request_id: int | None = None,
                available_skills: list | None = None,
                available_subagents: list | None = None,
            ) -> str:
                skill_name = available_skills[0]["name"] if available_skills else "none"
                return f"{args}|{issue_iid}|{merge_request_id}|{skill_name}"

        registry = SlashCommandRegistry()
        registry.register(DemoSlashCommand, "demo", [Scope.GLOBAL])

        middleware = SkillsMiddleware(backend=Mock(), sources=["/skills"])
        context = Mock()
        context.bot_username = "daiv"
        context.scope = Scope.GLOBAL
        context.repository = Mock(slug="repo-1")
        context.issue = Mock(iid=101)
        context.merge_request = Mock(merge_request_id=202)

        with patch("automation.agent.middlewares.skills.slash_command_registry", registry):
            result = await middleware._apply_builtin_slash_commands(
                [HumanMessage(content="/demo arg1")], context, [{"name": "skill-one"}]
            )

        assert result is not None
        assert result["jump_to"] == "end"
        assert isinstance(result["messages"][0], AIMessage)
        assert result["messages"][0].content == "arg1|101|202|skill-one"

    async def test_apply_builtin_slash_commands_returns_error_message_on_failure(self):
        class FailingSlashCommand(SlashCommand):
            description = "fail"

            async def execute_for_agent(
                self,
                *,
                args: str,
                issue_iid: int | None = None,
                merge_request_id: int | None = None,
                available_skills: list | None = None,
                available_subagents: list | None = None,
            ) -> str:
                raise RuntimeError("boom")

        registry = SlashCommandRegistry()
        registry.register(FailingSlashCommand, "fail", [Scope.GLOBAL])

        middleware = SkillsMiddleware(backend=Mock(), sources=["/skills"])
        context = Mock()
        context.bot_username = "daiv"
        context.scope = Scope.GLOBAL
        context.repository = Mock(slug="repo-1")
        context.issue = None
        context.merge_request = None

        with patch("automation.agent.middlewares.skills.slash_command_registry", registry):
            result = await middleware._apply_builtin_slash_commands(
                [HumanMessage(content="/fail now")], context, [{"name": "skill-one"}]
            )

        assert result is not None
        assert result["jump_to"] == "end"
        assert isinstance(result["messages"][0], AIMessage)
        assert result["messages"][0].content == "Failed to execute `/fail now`."

    async def test_apply_builtin_slash_commands_returns_none_for_ambiguous_command(self):
        class DemoSlashCommand(SlashCommand):
            description = "demo"

        class OtherSlashCommand(SlashCommand):
            description = "other"

        DemoSlashCommand.command = "demo"
        OtherSlashCommand.command = "demo"

        registry = Mock()
        registry.get_commands.return_value = [DemoSlashCommand, OtherSlashCommand]

        middleware = SkillsMiddleware(backend=Mock(), sources=["/skills"])
        context = Mock()
        context.bot_username = "daiv"
        context.scope = Scope.GLOBAL
        context.repository = Mock(slug="repo-1")
        context.issue = None
        context.merge_request = None

        with patch("automation.agent.middlewares.skills.slash_command_registry", registry):
            result = await middleware._apply_builtin_slash_commands(
                [HumanMessage(content="/demo now")], context, [{"name": "skill-one"}]
            )

        assert result is None

    async def test_skill_tool_reports_missing_skill(self):
        backend = Mock()
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])
        tool = middleware._skill_tool_generator()

        runtime = Mock()
        runtime.state = {"skills_metadata": [{"name": "demo", "path": "/skills/demo/SKILL.md"}]}
        runtime.tool_call_id = "call_1"

        result = await tool.coroutine(skill="missing", runtime=runtime)
        assert result == "error: Skill 'missing' not found. Available skills: demo."

    async def test_skill_tool_reports_download_failure(self):
        backend = Mock()
        backend.adownload_files = AsyncMock(return_value=[Mock(error="boom", content=b"")])
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])
        tool = middleware._skill_tool_generator()

        runtime = Mock()
        runtime.state = {"skills_metadata": [{"name": "demo", "path": "/skills/demo/SKILL.md"}]}
        runtime.tool_call_id = "call_1"

        result = await tool.coroutine(skill="demo", runtime=runtime)
        assert result == "error: Failed to launch skill 'demo': boom."

    async def test_skill_tool_formats_body_with_arguments(self):
        backend = Mock()
        backend.adownload_files = AsyncMock(
            return_value=[
                Mock(
                    error=None,
                    content=(b"---\nname: demo\ndescription: Demo\n---\nFirst $1, second $2, all: $ARGUMENTS"),
                )
            ]
        )
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])
        tool = middleware._skill_tool_generator()

        runtime = Mock()
        runtime.state = {"skills_metadata": [{"name": "demo", "path": "/skills/demo/SKILL.md"}]}
        runtime.tool_call_id = "call_1"

        result = await tool.coroutine(skill="demo", runtime=runtime, skill_args="alpha beta")
        assert isinstance(result, Command)
        messages = result.update["messages"]
        assert isinstance(messages[0], ToolMessage)
        assert messages[0].content == "Launching skill 'demo'..."
        assert isinstance(messages[1], HumanMessage)
        assert messages[1].content == "First alpha, second beta, all: alpha beta"

    async def test_skill_tool_appends_named_arguments_when_missing_placeholder(self):
        backend = Mock()
        backend.adownload_files = AsyncMock(
            return_value=[Mock(error=None, content=b"---\nname: demo\ndescription: Demo\n---\nRun this.")]
        )
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])
        tool = middleware._skill_tool_generator()

        runtime = Mock()
        runtime.state = {"skills_metadata": [{"name": "demo", "path": "/skills/demo/SKILL.md"}]}
        runtime.tool_call_id = "call_1"

        result = await tool.coroutine(skill="demo", runtime=runtime, skill_args="--flag=1")
        assert isinstance(result, Command)
        messages = result.update["messages"]
        assert messages[1].content.endswith("\n\n$ARGUMENTS: --flag=1")

    async def test_abefore_agent_skips_slash_commands_when_disabled(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="does one"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])
        runtime = _make_runtime(repo_working_dir=str(tmp_path / repo_name))
        runtime.context.config = RepositoryConfig(slash_commands=SlashCommands(enabled=False))

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            patch.object(middleware, "_apply_builtin_slash_commands") as mock_apply,
        ):
            await middleware.abefore_agent({"messages": [HumanMessage(content="/help")]}, runtime, Mock())

        mock_apply.assert_not_called()

    async def test_discovers_skills_from_multiple_sources(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="builtin one"))

        # Per-repo skills committed inside the repo working tree.
        daiv_skill = tmp_path / repo_name / AGENTS_SKILLS_PATH / "daiv-skill"
        daiv_skill.mkdir(parents=True)
        (daiv_skill / "SKILL.md").write_text(_make_skill_md(name="daiv-skill", description="from daiv"))

        agents_skill = tmp_path / repo_name / CLAUDE_CODE_SKILLS_PATH / "agents-skill"
        agents_skill.mkdir(parents=True)
        (agents_skill / "SKILL.md").write_text(_make_skill_md(name="agents-skill", description="from agents"))

        cursor_skill = tmp_path / repo_name / CURSOR_SKILLS_PATH / "cursor-skill"
        cursor_skill.mkdir(parents=True)
        (cursor_skill / "SKILL.md").write_text(_make_skill_md(name="cursor-skill", description="from cursor"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(
            backend=backend, sources=["/skills", *[f"/{repo_name}/{source}" for source in SKILLS_SOURCES]]
        )
        runtime = _make_runtime(repo_working_dir=str(tmp_path / repo_name))

        with patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin):
            result = await middleware.abefore_agent({"messages": [HumanMessage(content="hello")]}, runtime, Mock())

        assert result is not None
        skills = {skill["name"]: skill for skill in result["skills_metadata"]}
        assert set(skills) == {"skill-one", "daiv-skill", "agents-skill", "cursor-skill"}
        assert skills["skill-one"]["description"] == "builtin one"
        assert skills["skill-one"]["metadata"]["is_builtin"] is True
        assert skills["daiv-skill"]["description"] == "from daiv"
        assert "is_builtin" not in skills["daiv-skill"]["metadata"]
        assert skills["agents-skill"]["description"] == "from agents"
        assert "is_builtin" not in skills["agents-skill"]["metadata"]
        assert skills["cursor-skill"]["description"] == "from cursor"
        assert "is_builtin" not in skills["cursor-skill"]["metadata"]


class TestCustomGlobalSkills:
    """Tests for custom global skills support."""

    async def test_copies_custom_global_skills(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="builtin one"))

        custom_global = tmp_path / "custom_skills"
        (custom_global / "my-global-skill").mkdir(parents=True)
        (custom_global / "my-global-skill" / "SKILL.md").write_text(
            _make_skill_md(name="my-global-skill", description="a global skill")
        )

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])
        runtime = _make_runtime(repo_working_dir=str(tmp_path / repo_name))

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.skills.agent_settings.CUSTOM_SKILLS_PATH", custom_global),
        ):
            result = await middleware.abefore_agent({"messages": [HumanMessage(content="hello")]}, runtime, Mock())

        assert result is not None
        skills = {skill["name"]: skill for skill in result["skills_metadata"]}
        assert set(skills) == {"skill-one", "my-global-skill"}
        assert skills["my-global-skill"]["description"] == "a global skill"
        assert skills["my-global-skill"]["metadata"].get("is_global") is True
        assert skills["skill-one"]["metadata"].get("is_builtin") is True

    async def test_custom_global_skill_overrides_builtin(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "plan").mkdir(parents=True)
        (builtin / "plan" / "SKILL.md").write_text(_make_skill_md(name="plan", description="builtin plan"))

        custom_global = tmp_path / "custom_skills"
        (custom_global / "plan").mkdir(parents=True)
        (custom_global / "plan" / "SKILL.md").write_text(_make_skill_md(name="plan", description="custom plan"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])
        runtime = _make_runtime(repo_working_dir=str(tmp_path / repo_name))

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.skills.agent_settings.CUSTOM_SKILLS_PATH", custom_global),
        ):
            result = await middleware.abefore_agent({"messages": [HumanMessage(content="hello")]}, runtime, Mock())

        assert result is not None
        skills = {skill["name"]: skill for skill in result["skills_metadata"]}
        assert skills["plan"]["description"] == "custom plan"
        assert skills["plan"]["metadata"].get("is_global") is True
        assert "is_builtin" not in skills["plan"]["metadata"]

    async def test_custom_global_skill_marked_as_global(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        builtin.mkdir(parents=True)

        custom_global = tmp_path / "custom_skills"
        (custom_global / "global-skill").mkdir(parents=True)
        (custom_global / "global-skill" / "SKILL.md").write_text(
            _make_skill_md(name="global-skill", description="a global skill")
        )

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.skills.agent_settings.CUSTOM_SKILLS_PATH", custom_global),
        ):
            builtin_names, custom_global_names = await middleware._copy_global_skills(agent_path=tmp_path / repo_name)

        assert "global-skill" in custom_global_names
        assert "global-skill" not in builtin_names

    async def test_per_repo_skill_overrides_custom_global(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        builtin.mkdir(parents=True)

        custom_global = tmp_path / "custom_skills"
        (custom_global / "shared-skill").mkdir(parents=True)
        (custom_global / "shared-skill" / "SKILL.md").write_text(
            _make_skill_md(name="shared-skill", description="global version")
        )

        # Per-repo skill committed in the repo working tree at `<repo>/.agents/skills/`.
        repo_skill = tmp_path / repo_name / AGENTS_SKILLS_PATH / "shared-skill"
        repo_skill.mkdir(parents=True)
        (repo_skill / "SKILL.md").write_text(_make_skill_md(name="shared-skill", description="repo version"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills", f"/{repo_name}/{AGENTS_SKILLS_PATH}"])
        runtime = _make_runtime(repo_working_dir=str(tmp_path / repo_name))

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.skills.agent_settings.CUSTOM_SKILLS_PATH", custom_global),
        ):
            result = await middleware.abefore_agent({"messages": [HumanMessage(content="hello")]}, runtime, Mock())

        assert result is not None
        skills = {skill["name"]: skill for skill in result["skills_metadata"]}
        # Per-repo content wins (last source wins); metadata still labels by name-registration origin.
        assert skills["shared-skill"]["description"] == "repo version"
        assert skills["shared-skill"]["metadata"].get("is_global") is True
        assert "is_builtin" not in skills["shared-skill"]["metadata"]

    async def test_custom_global_skills_disabled_when_path_is_none(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="builtin one"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.skills.agent_settings.CUSTOM_SKILLS_PATH", None),
        ):
            builtin_names, custom_global_names = await middleware._copy_global_skills(agent_path=tmp_path / repo_name)

        assert builtin_names == ["skill-one"]
        assert custom_global_names == []

    async def test_custom_global_skills_skipped_when_path_not_exists(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="builtin one"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            patch("automation.agent.middlewares.skills.agent_settings.CUSTOM_SKILLS_PATH", tmp_path / "nonexistent"),
        ):
            builtin_names, custom_global_names = await middleware._copy_global_skills(agent_path=tmp_path / repo_name)

        assert builtin_names == ["skill-one"]
        assert custom_global_names == []
