from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from skills.models import GlobalSkill, SkillInvocation
from skills.services import _classify_source, global_skill_dirs, list_builtins, list_global_skills


def _write_skill(root, name, description):
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n")


@pytest.fixture
def skill_roots(tmp_path, monkeypatch):
    """Point the built-in and custom global skill roots at empty temp dirs."""
    builtin, custom = tmp_path / "builtin", tmp_path / "custom"
    builtin.mkdir()
    custom.mkdir()
    monkeypatch.setattr("skills.services.BUILTIN_SKILLS_PATH", builtin)
    monkeypatch.setattr("skills.services.settings.CUSTOM_SKILLS_PATH", custom)
    return builtin, custom


def test_list_global_skills_reads_builtin_and_custom(skill_roots):
    builtin, custom = skill_roots
    _write_skill(builtin, "code-review", "Review a diff")
    _write_skill(custom, "deploy", "Deploy the app")

    assert list_global_skills() == {"code-review": "Review a diff", "deploy": "Deploy the app"}


def test_custom_global_skill_shadows_builtin(skill_roots):
    builtin, custom = skill_roots
    _write_skill(builtin, "shared", "builtin version")
    _write_skill(custom, "shared", "custom version")

    assert global_skill_dirs() == {"shared": custom / "shared"}
    assert list_global_skills() == {"shared": "custom version"}


def test_global_skill_dirs_skip_storage_dirs_and_pycache(skill_roots):
    builtin, custom = skill_roots
    _write_skill(builtin, "plan", "x")
    (builtin / "__pycache__").mkdir()
    _write_skill(custom, "demo", "real skill")
    _write_skill(custom / ".trash", "demo.123", "trashed copy")
    _write_skill(custom / ".tmp", "leftover", "staging leftover")
    (custom / ".zips").mkdir()

    assert global_skill_dirs() == {"demo": custom / "demo", "plan": builtin / "plan"}


@pytest.mark.parametrize("custom", [None, "missing"])
def test_global_skill_dirs_keep_builtins_without_a_custom_root(skill_roots, monkeypatch, custom):
    builtin, _ = skill_roots
    _write_skill(builtin, "only-builtin", "x")
    monkeypatch.setattr(
        "skills.services.settings.CUSTOM_SKILLS_PATH", None if custom is None else builtin.parent / custom
    )

    assert global_skill_dirs() == {"only-builtin": builtin / "only-builtin"}


def test_global_skill_dirs_skip_an_unreadable_root(skill_roots, monkeypatch):
    builtin, _ = skill_roots
    _write_skill(builtin, "code-review", "x")
    unreadable = MagicMock()
    unreadable.iterdir.side_effect = PermissionError("denied")
    monkeypatch.setattr("skills.services.settings.CUSTOM_SKILLS_PATH", unreadable)

    assert global_skill_dirs() == {"code-review": builtin / "code-review"}


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


@pytest.mark.django_db(transaction=True)
async def test_classify_source_builtin_when_no_override_row():
    # No GlobalSkill row for this name → built-in resolution wins.
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset({"code-review"})):
        assert (
            await _classify_source("code-review", "/workspace/skills/code-review/SKILL.md")
            == SkillInvocation.Source.BUILTIN
        )


@pytest.mark.django_db(transaction=True)
async def test_classify_source_global_overrides_builtin_when_row_exists():
    """When a custom global skill shadows a built-in of the same name, the
    invocation must be classified GLOBAL — that's the version the agent actually
    ran. Classifying as BUILTIN here would split the counts and hide overrides
    from telemetry."""
    await GlobalSkill.objects.acreate(name="code-review", description="x", size_bytes=1, file_count=1, checksum="c")
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset({"code-review"})):
        assert (
            await _classify_source("code-review", "/workspace/skills/code-review/SKILL.md")
            == SkillInvocation.Source.GLOBAL
        )


@pytest.mark.django_db(transaction=True)
async def test_classify_source_global_when_path_under_global_skills_path():
    await GlobalSkill.objects.acreate(name="custom-thing", description="x", size_bytes=1, file_count=1, checksum="c")
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset()):
        assert (
            await _classify_source("custom-thing", "/workspace/skills/custom-thing/SKILL.md")
            == SkillInvocation.Source.GLOBAL
        )


@pytest.mark.django_db(transaction=True)
async def test_classify_source_repo_when_path_outside_global_skills_path():
    # Pin the invariant that SKILLS_PATH is the only gate to BUILTIN —
    # a per-repo skill whose name happens to match a built-in must still be
    # classified REPO, so per-repo overrides aren't mis-attributed in telemetry.
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset({"repo-skill"})):
        assert (
            await _classify_source("repo-skill", "/workspace/repo/.agents/skills/repo-skill/SKILL.md")
            == SkillInvocation.Source.REPO
        )


@pytest.mark.django_db(transaction=True)
async def testrecord_invocation_warns_on_unclassified_path(caplog):
    """A REPO classification on a path that doesn't look like a per-repo
    .agents/skills layout should surface a warning so a new skill location
    doesn't silently land in the REPO bucket."""
    import logging

    from skills.services import record_invocation

    rt = _runtime()
    caplog.set_level(logging.WARNING, logger="daiv.skills")
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset()):
        await record_invocation(name="weird", skill_path="/some/weird/location/weird/SKILL.md", runtime=rt)
    assert any("unclassified skill path" in rec.message for rec in caplog.records)


def _runtime(*, repo_slug: str = "org/repo", thread_id: str | None = None) -> MagicMock:
    rt = MagicMock()
    rt.context.repository.slug = repo_slug
    rt.config = {"configurable": {"thread_id": thread_id or str(uuid.uuid4())}}
    return rt


@pytest.mark.django_db(transaction=True)
async def testrecord_invocation_creates_row():
    from skills.services import record_invocation

    tid = str(uuid.uuid4())
    rt = _runtime(repo_slug="org/repo", thread_id=tid)
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset({"code-review"})):
        await record_invocation(name="code-review", skill_path="/workspace/skills/code-review/SKILL.md", runtime=rt)
    row = await SkillInvocation.objects.aget()
    assert row.name == "code-review"
    assert row.source == SkillInvocation.Source.BUILTIN
    assert row.repo_slug == "org/repo"
    assert str(row.thread_id) == tid


@pytest.mark.django_db(transaction=True)
async def testrecord_invocation_swallows_db_errors(caplog):
    from django.db import DatabaseError

    from skills.services import record_invocation

    rt = _runtime()
    with (
        patch("skills.services.BUILTIN_SKILL_NAMES", frozenset()),
        patch.object(SkillInvocation.objects, "acreate", new=AsyncMock(side_effect=DatabaseError("db down"))),
    ):
        # Must not raise — telemetry failure cannot abort a skill invocation.
        await record_invocation(name="anything", skill_path="/workspace/skills/anything/SKILL.md", runtime=rt)
    assert any("Failed to record skill invocation" in rec.message for rec in caplog.records)


@pytest.mark.django_db(transaction=True)
async def testrecord_invocation_swallows_missing_thread_id(caplog):
    from skills.services import record_invocation

    rt = MagicMock()
    rt.context.repository.slug = "org/repo"
    rt.config = {"configurable": {}}  # no thread_id
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset()):
        await record_invocation(name="anything", skill_path="/workspace/skills/anything/SKILL.md", runtime=rt)
    # No row created and the failure was logged.
    assert await SkillInvocation.objects.acount() == 0
    assert any("Failed to record skill invocation" in rec.message for rec in caplog.records)


@pytest.mark.django_db(transaction=True)
async def testrecord_invocation_swallows_none_config(caplog):
    """A None runtime.config subscripted yields TypeError, not KeyError. The
    handler must still swallow it so a broken runtime can't abort the agent."""
    from skills.services import record_invocation

    rt = MagicMock()
    rt.context.repository.slug = "org/repo"
    rt.config = None
    with patch("skills.services.BUILTIN_SKILL_NAMES", frozenset()):
        await record_invocation(name="anything", skill_path="/workspace/skills/anything/SKILL.md", runtime=rt)
    assert await SkillInvocation.objects.acount() == 0
    assert any("Failed to record skill invocation" in rec.message for rec in caplog.records)
