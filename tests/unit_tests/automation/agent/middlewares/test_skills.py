from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from automation.agent.middlewares.skills import SkillsMiddleware


def _make_runtime(*, repo_working_dir: str) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.repo = Mock()
    runtime.context.repo.working_dir = repo_working_dir
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

    async def test_skips_when_skills_metadata_present(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=[f"/{repo_name}/.daiv/skills"])
        runtime = _make_runtime(repo_working_dir=str(tmp_path / repo_name))

        result = await middleware.abefore_agent({"skills_metadata": []}, runtime, Mock())
        assert result is None
        assert not (tmp_path / repo_name / ".daiv" / "skills").exists()

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
        middleware = SkillsMiddleware(backend=backend, sources=[f"/{repo_name}/.daiv/skills"])
        runtime = _make_runtime(repo_working_dir=str(tmp_path / repo_name))

        with patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin):
            result = await middleware.abefore_agent({}, runtime, Mock())

        assert result is not None
        skills = {skill["name"]: skill for skill in result["skills_metadata"]}
        assert set(skills) == {"skill-one", "skill-two"}
        assert skills["skill-one"]["description"] == "does one"
        assert skills["skill-two"]["description"] == "does two"
        assert skills["skill-one"]["path"] == f"/{repo_name}/.daiv/skills/skill-one/SKILL.md"
        assert skills["skill-two"]["path"] == f"/{repo_name}/.daiv/skills/skill-two/SKILL.md"
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

        custom_skill = tmp_path / repo_name / ".daiv" / "skills" / "custom-skill"
        custom_skill.mkdir(parents=True)
        (custom_skill / "SKILL.md").write_text(
            _make_skill_md(
                name="custom-skill", description="does custom", metadata={"is_builtin": "true", "owner": "user"}
            )
        )

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=[f"/{repo_name}/.daiv/skills"])
        runtime = _make_runtime(repo_working_dir=str(tmp_path / repo_name))

        with patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin):
            result = await middleware.abefore_agent({}, runtime, Mock())

        assert result is not None
        skills = {skill["name"]: skill for skill in result["skills_metadata"]}
        assert skills["skill-one"]["metadata"]["is_builtin"] is True
        assert skills["skill-two"]["metadata"]["is_builtin"] is True
        assert skills["custom-skill"]["metadata"]["owner"] == "user"
        assert "is_builtin" not in skills["custom-skill"]["metadata"]

    async def test_uploads_missing_files_and_gitignore(self, tmp_path: Path):
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
        middleware = SkillsMiddleware(backend=backend, sources=[f"/{repo_name}/.daiv/skills"])

        with patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin):
            await middleware._copy_builtin_skills(agent_path=tmp_path / repo_name)

        project_skills = tmp_path / repo_name / ".daiv" / "skills"
        assert (project_skills / "skill-one" / "SKILL.md").read_text() == _make_skill_md(
            name="skill-one", description="does one"
        )
        assert (project_skills / "skill-one" / "helpers" / "util.py").read_text() == "print('one')\n"
        assert (project_skills / "skill-two" / "SKILL.md").read_text() == _make_skill_md(
            name="skill-two", description="does two"
        )
        assert (project_skills / "skill-one" / ".gitignore").read_text() == "*"
        assert (project_skills / "skill-two" / ".gitignore").read_text() == "*"
        assert not (project_skills / "__pycache__").exists()
        assert not any(p.name == "not_a_dir.txt" for p in project_skills.rglob("*"))

    async def test_skips_file_upload_when_dest_exists(self, tmp_path: Path):
        from deepagents.backends.filesystem import FilesystemBackend

        repo_name = "repoX"
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one" / "helpers").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="builtin"))
        (builtin / "skill-one" / "helpers" / "util.py").write_text("print('one')\n")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = SkillsMiddleware(backend=backend, sources=[f"/{repo_name}/.daiv/skills"])

        project_skill_md = tmp_path / repo_name / ".daiv" / "skills" / "skill-one" / "SKILL.md"
        project_skill_md.parent.mkdir(parents=True, exist_ok=True)
        project_skill_md.write_text(_make_skill_md(name="skill-one", description="existing"))

        original_exists = Path.exists

        def fake_exists(self: Path) -> bool:
            # In production this path is real (repo mounted at /repoX). In tests we map it into tmp_path.
            if str(self).startswith(f"/{repo_name}/"):
                mapped = tmp_path / str(self).lstrip("/")
                return original_exists(mapped)
            return original_exists(self)

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            patch("pathlib.Path.exists", new=fake_exists),
        ):
            await middleware._copy_builtin_skills(agent_path=tmp_path / repo_name)

        # SKILL.md should not be overwritten, but other files should still be uploaded.
        assert project_skill_md.read_text() == _make_skill_md(name="skill-one", description="existing")
        assert (tmp_path / repo_name / ".daiv" / "skills" / "skill-one" / "helpers" / "util.py").read_text() == (
            "print('one')\n"
        )
        assert (tmp_path / repo_name / ".daiv" / "skills" / "skill-one" / ".gitignore").read_text() == "*"

    async def test_raises_when_backend_returns_error(self, tmp_path: Path):
        builtin = tmp_path / "builtin_skills"
        (builtin / "skill-one").mkdir(parents=True)
        (builtin / "skill-one" / "SKILL.md").write_text(_make_skill_md(name="skill-one", description="does one"))

        backend = Mock()
        backend.aupload_files = AsyncMock(return_value=[Mock(error="boom")])
        middleware = SkillsMiddleware(backend=backend, sources=["/skills"])

        with (
            patch("automation.agent.middlewares.skills.BUILTIN_SKILLS_PATH", builtin),
            pytest.raises(RuntimeError, match="Failed to upload builtin skill: boom"),
        ):
            await middleware._copy_builtin_skills(agent_path=tmp_path / "repoX")

    def test_format_skills_list_marks_builtin(self):
        middleware = SkillsMiddleware(backend=Mock(), sources=["/skills"])
        formatted = middleware._format_skills_list([
            {
                "name": "skill-one",
                "description": "does one",
                "path": "/skills/skill-one/SKILL.md",
                "metadata": {"is_builtin": True},
            },
            {
                "name": "custom-skill",
                "description": "does custom",
                "path": "/skills/custom-skill/SKILL.md",
                "metadata": {},
            },
        ])

        lines = formatted.splitlines()
        assert lines[0] == "- **skill-one (Builtin)**: does one"
        assert lines[1] == "  -> Read `/skills/skill-one/SKILL.md` for full instructions"
        assert lines[2] == "- **custom-skill**: does custom"
        assert lines[3] == "  -> Read `/skills/custom-skill/SKILL.md` for full instructions"
