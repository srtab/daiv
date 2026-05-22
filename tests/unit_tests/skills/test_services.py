from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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


def _runtime(*, repo_slug: str = "org/repo", thread_id: str | None = None) -> MagicMock:
    rt = MagicMock()
    rt.context.repository.slug = repo_slug
    rt.config = {"configurable": {"thread_id": thread_id or str(uuid.uuid4())}}
    return rt


@pytest.mark.django_db(transaction=True)
async def test_record_invocation_creates_row():
    from skills.services import _record_invocation

    tid = str(uuid.uuid4())
    rt = _runtime(repo_slug="org/repo", thread_id=tid)
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset({"code-review"})):
        await _record_invocation(name="code-review", skill_path="/skills/code-review/SKILL.md", runtime=rt)
    row = await SkillInvocation.objects.aget()
    assert row.name == "code-review"
    assert row.source == SkillInvocation.Source.BUILTIN
    assert row.repo_slug == "org/repo"
    assert str(row.thread_id) == tid


@pytest.mark.django_db(transaction=True)
async def test_record_invocation_swallows_db_errors(caplog):
    from skills.services import _record_invocation

    rt = _runtime()
    with (
        patch("skills.services.BUILTIN_SKILL_NAMES", frozenset()),
        patch.object(SkillInvocation.objects, "acreate", new=AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        # Must not raise — telemetry failure cannot abort a skill invocation.
        await _record_invocation(name="anything", skill_path="/skills/anything/SKILL.md", runtime=rt)
    assert any("Failed to record skill invocation" in rec.message for rec in caplog.records)


@pytest.mark.django_db(transaction=True)
async def test_record_invocation_swallows_missing_thread_id(caplog):
    from skills.services import _record_invocation

    rt = MagicMock()
    rt.context.repository.slug = "org/repo"
    rt.config = {"configurable": {}}  # no thread_id
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset()):
        await _record_invocation(name="anything", skill_path="/skills/anything/SKILL.md", runtime=rt)
    # No row created and the failure was logged.
    assert await SkillInvocation.objects.acount() == 0
    assert any("Failed to record skill invocation" in rec.message for rec in caplog.records)
