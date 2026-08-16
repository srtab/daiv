from __future__ import annotations

import pytest
from skills.models import GlobalSkill
from skills.services import list_builtins

from slash_commands.composer import composer_command_rows


@pytest.fixture
def builtin_skills_root(tmp_path, monkeypatch, request):
    """Point ``list_builtins`` at a controlled tree: two skills so ordering is observable."""
    builtin = tmp_path / "builtin_skills"
    for name, description in (("zeta-skill", "last alphabetically"), ("alpha-skill", "first alphabetically")):
        (builtin / name).mkdir(parents=True)
        (builtin / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n")
    monkeypatch.setattr("skills.services.BUILTIN_SKILLS_PATH", builtin)
    # list_builtins is lru_cached: clear before populating it against tmp_path, and
    # register a teardown so later tests don't see entries from a deleted tmp dir.
    list_builtins.cache_clear()
    request.addfinalizer(list_builtins.cache_clear)
    return builtin


@pytest.mark.django_db
def test_global_commands_included_with_kind():
    commands = {row["name"]: row for row in composer_command_rows() if row["kind"] == "command"}

    assert "help" in commands
    assert "agents" in commands
    assert commands["help"]["description"]


@pytest.mark.django_db
def test_issue_only_commands_excluded():
    """The chat runs at GLOBAL scope; commands registered only for issues/MRs must not
    be offered in the composer."""
    names = {row["name"] for row in composer_command_rows() if row["kind"] == "command"}

    assert "clear" not in names
    assert "clone-to-topics" not in names


@pytest.mark.django_db
def test_builtin_skills_included(builtin_skills_root):
    skills = {row["name"]: row for row in composer_command_rows() if row["kind"] == "skill"}

    assert skills["alpha-skill"]["description"] == "first alphabetically"
    assert skills["zeta-skill"]["description"] == "last alphabetically"


@pytest.mark.django_db
def test_global_skill_shadows_builtin(builtin_skills_root):
    GlobalSkill.objects.create(
        name="alpha-skill", description="custom override", size_bytes=1, file_count=1, checksum="x"
    )

    rows = [row for row in composer_command_rows() if row["name"] == "alpha-skill"]

    assert len(rows) == 1
    assert rows[0]["description"] == "custom override"


@pytest.mark.django_db
def test_custom_skill_without_builtin_counterpart_included(builtin_skills_root):
    GlobalSkill.objects.create(name="brand-new", description="uploaded", size_bytes=1, file_count=1, checksum="x")

    skills = {row["name"] for row in composer_command_rows() if row["kind"] == "skill"}

    assert "brand-new" in skills


@pytest.mark.django_db
def test_ordering_commands_first_then_skills_alphabetical(builtin_skills_root):
    rows = composer_command_rows()

    kinds = [row["kind"] for row in rows]
    assert kinds == ["command"] * kinds.count("command") + ["skill"] * kinds.count("skill")
    command_names = [row["name"] for row in rows if row["kind"] == "command"]
    skill_names = [row["name"] for row in rows if row["kind"] == "skill"]
    assert command_names == sorted(command_names)
    assert skill_names == sorted(skill_names)
