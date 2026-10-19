from __future__ import annotations

import pytest

from slash_commands.composer import composer_command_rows


def _write_skill(root, name, description):
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n")


@pytest.fixture
def custom_skills_root(tmp_path, monkeypatch):
    """Point the global skills at controlled trees: two built-ins so ordering is observable, and an empty custom
    root the test writes uploads into."""
    builtin = tmp_path / "builtin_skills"
    for name, description in (("zeta-skill", "last alphabetically"), ("alpha-skill", "first alphabetically")):
        _write_skill(builtin, name, description)
    custom = tmp_path / "custom_skills"
    custom.mkdir()
    monkeypatch.setattr("skills.services.BUILTIN_SKILLS_PATH", builtin)
    monkeypatch.setattr("skills.services.settings.CUSTOM_SKILLS_PATH", custom)
    return custom


def test_global_commands_included_with_kind():
    commands = {row["name"]: row for row in composer_command_rows() if row["kind"] == "command"}

    assert "help" in commands
    assert "agents" in commands
    assert commands["help"]["description"]


def test_issue_only_commands_excluded():
    """The chat runs at GLOBAL scope; commands registered only for issues/MRs must not
    be offered in the composer."""
    names = {row["name"] for row in composer_command_rows() if row["kind"] == "command"}

    assert "clear" not in names
    assert "clone-to-topics" not in names


def test_builtin_skills_included(custom_skills_root):
    skills = {row["name"]: row for row in composer_command_rows() if row["kind"] == "skill"}

    assert skills["alpha-skill"]["description"] == "first alphabetically"
    assert skills["zeta-skill"]["description"] == "last alphabetically"


def test_custom_skill_shadows_builtin(custom_skills_root):
    _write_skill(custom_skills_root, "alpha-skill", "custom override")

    rows = [row for row in composer_command_rows() if row["name"] == "alpha-skill"]

    assert len(rows) == 1
    assert rows[0]["description"] == "custom override"


def test_custom_skill_without_builtin_counterpart_included(custom_skills_root):
    _write_skill(custom_skills_root, "brand-new", "uploaded")

    skills = {row["name"] for row in composer_command_rows() if row["kind"] == "skill"}

    assert "brand-new" in skills


def test_ordering_commands_first_then_skills_alphabetical(custom_skills_root):
    rows = composer_command_rows()

    kinds = [row["kind"] for row in rows]
    assert kinds.index("skill") == kinds.count("command")
    command_names = [row["name"] for row in rows if row["kind"] == "command"]
    skill_names = [row["name"] for row in rows if row["kind"] == "skill"]
    assert command_names == sorted(command_names)
    assert skill_names == sorted(skill_names)
