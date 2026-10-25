from __future__ import annotations

import pytest
from plugins.conf import settings as plugin_settings
from plugins.fetch import revision_path
from plugins.manifest import ItemRef, PluginContents
from plugins.models import Plugin

from slash_commands.composer import DESCRIPTION_CHARS, composer_command_rows
from tests.unit_tests.conftest import write_skill

pytestmark = pytest.mark.django_db

URL = "https://git.example.com/group/repo.git"
SHA = "a" * 40


@pytest.fixture
def data_path(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_settings, "DATA_PATH", tmp_path)
    return tmp_path


def install_plugin(name, user=None, skill="brainstorming", description="Explore ideas"):
    contents = PluginContents(
        name=name,
        description="",
        version="1",
        skills=(ItemRef(skill, description, f"skills/{skill}/SKILL.md"),),
        commands=(),
        agents=(),
        skipped=(),
    )
    plugin = Plugin.objects.create(
        scope=Plugin.Scope.USER if user else Plugin.Scope.GLOBAL,
        user=user,
        name=name,
        repo_url=URL if user is None else f"{URL}?{name}",
        installed_commit=SHA,
        contents=contents.to_json(),
    )
    revision_path(plugin.repo_url, SHA).mkdir(parents=True, exist_ok=True)
    return plugin


@pytest.fixture
def custom_skills_root(skill_roots):
    """Two built-ins so ordering is observable, and the empty custom root the test writes uploads into."""
    builtin, custom = skill_roots
    write_skill(builtin, "zeta-skill", "last alphabetically")
    write_skill(builtin, "alpha-skill", "first alphabetically")
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
    write_skill(custom_skills_root, "alpha-skill", "custom override")

    rows = [row for row in composer_command_rows() if row["name"] == "alpha-skill"]

    assert len(rows) == 1
    assert rows[0]["description"] == "custom override"


def test_custom_skill_without_builtin_counterpart_included(custom_skills_root):
    write_skill(custom_skills_root, "brand-new", "uploaded")

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


def test_composer_includes_users_plugin_skills(custom_skills_root, member_user, data_path):
    install_plugin("superpowers", member_user, description="x" * 500)

    rows = composer_command_rows(member_user)

    skills = [row for row in rows if row["kind"] == "skill"]
    assert [row["name"] for row in skills] == ["alpha-skill", "zeta-skill", "superpowers:brainstorming"]
    assert len(skills[-1]["description"]) <= DESCRIPTION_CHARS


def test_composer_excludes_other_users_plugins(custom_skills_root, member_user, admin_user, data_path):
    install_plugin("superpowers", member_user)

    names = {row["name"] for row in composer_command_rows(admin_user)}

    assert "superpowers:brainstorming" not in names


def test_composer_without_user_has_no_personal_plugins(custom_skills_root, member_user, data_path):
    install_plugin("mine", member_user)
    install_plugin("shared")

    names = {row["name"] for row in composer_command_rows()}

    assert "mine:brainstorming" not in names
    assert "shared:brainstorming" in names
