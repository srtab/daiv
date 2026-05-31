from typing import TYPE_CHECKING
from unittest.mock import patch

from automation.agent.middlewares.slash_commands import _load_global_skill_metadata

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
