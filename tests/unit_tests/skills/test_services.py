from __future__ import annotations

from unittest.mock import patch

from skills.models import SkillInvocation
from skills.services import _classify_source, list_builtins


def test_list_builtins_returns_dicts_with_name_and_description():
    builtins = list_builtins()
    assert builtins, "expected at least one shipped built-in skill"
    sample = builtins[0]
    assert set(sample.keys()) >= {"name", "description"}
    # Sanity: every name is a non-empty string
    for entry in builtins:
        assert entry["name"]
        # Description may be empty if a built-in lacks frontmatter, but it
        # should always be a string.
        assert isinstance(entry["description"], str)


def test_classify_source_builtin_takes_priority():
    # Even when the path is under /skills/, a name in BUILTIN_SKILL_NAMES is built-in.
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset({"code-review"})):
        assert _classify_source("code-review", "/skills/code-review/SKILL.md") == SkillInvocation.Source.BUILTIN


def test_classify_source_global_when_path_under_global_skills_path():
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset()):
        assert _classify_source("custom-thing", "/skills/custom-thing/SKILL.md") == SkillInvocation.Source.GLOBAL


def test_classify_source_repo_when_path_outside_global_skills_path():
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset()):
        assert (
            _classify_source("repo-skill", "/workspace/repo/.agents/skills/repo-skill/SKILL.md")
            == SkillInvocation.Source.REPO
        )
