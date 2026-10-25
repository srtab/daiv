from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from skills.models import GlobalSkill, SkillInvocation
from skills.services import _classify_source, global_skill_dirs, global_skill_files, list_builtins, list_global_skills

from tests.unit_tests.conftest import write_skill


def test_list_global_skills_reads_builtin_and_custom(skill_roots):
    builtin, custom = skill_roots
    write_skill(builtin, "code-review", "Review a diff")
    write_skill(custom, "deploy", "Deploy the app")

    assert list_global_skills() == {"code-review": "Review a diff", "deploy": "Deploy the app"}


def test_custom_global_skill_shadows_builtin(skill_roots):
    builtin, custom = skill_roots
    write_skill(builtin, "shared", "builtin version")
    write_skill(custom, "shared", "custom version")

    assert global_skill_dirs() == ({"shared": custom / "shared"}, [])
    assert list_global_skills() == {"shared": "custom version"}


def test_global_skill_dirs_skip_storage_dirs_and_pycache(skill_roots):
    builtin, custom = skill_roots
    write_skill(builtin, "plan", "x")
    (builtin / "__pycache__").mkdir()
    write_skill(custom, "demo", "real skill")
    write_skill(custom / ".trash", "demo.123", "trashed copy")
    write_skill(custom / ".tmp", "leftover", "staging leftover")
    (custom / ".zips").mkdir()

    assert global_skill_dirs() == ({"demo": custom / "demo", "plan": builtin / "plan"}, [])


@pytest.mark.parametrize("custom", [None, "missing"])
def test_global_skill_dirs_keep_builtins_without_a_custom_root(skill_roots, monkeypatch, caplog, custom):
    builtin, _ = skill_roots
    write_skill(builtin, "only-builtin", "x")
    monkeypatch.setattr(
        "skills.services.settings.CUSTOM_SKILLS_PATH", None if custom is None else builtin.parent / custom
    )

    with caplog.at_level(logging.DEBUG, logger="daiv.skills"):
        assert global_skill_dirs() == ({"only-builtin": builtin / "only-builtin"}, [])
    assert caplog.records == []


def test_global_skill_dirs_report_an_unreadable_root(skill_roots, caplog, chmod):
    builtin, custom = skill_roots
    write_skill(builtin, "code-review", "x")
    write_skill(custom, "deploy", "x")
    chmod(custom, 0)

    with caplog.at_level(logging.ERROR, logger="daiv.skills"):
        dirs, errors = global_skill_dirs()

    assert dirs == {"code-review": builtin / "code-review"}
    assert errors == ["Some global skills failed to load: Permission denied"]
    (record,) = caplog.records
    assert str(custom) in record.getMessage()


def test_custom_dir_without_skill_md_does_not_shadow_the_builtin(skill_roots):
    builtin, custom = skill_roots
    write_skill(builtin, "plan", "builtin plan")
    (custom / "plan" / "scripts").mkdir(parents=True)

    dirs, errors = global_skill_dirs()

    assert dirs == {"plan": builtin / "plan"}
    assert errors == ["Cannot load skill 'plan': no readable SKILL.md"]
    assert list_global_skills() == {"plan": "builtin plan"}


def test_global_skill_files_leave_out_bytecode(skill_roots):
    builtin, _ = skill_roots
    write_skill(
        builtin,
        "plan",
        "x",
        {"scripts/run.py": "print()", "scripts/run.pyc": "", "scripts/__pycache__/run.cpython-314.pyc": ""},
    )

    dirs, files, errors = global_skill_files()

    assert list(dirs) == ["plan", "plan/scripts"]
    assert files == {
        "plan/SKILL.md": builtin / "plan" / "SKILL.md",
        "plan/scripts/run.py": builtin / "plan" / "scripts" / "run.py",
    }
    assert errors == []


@pytest.mark.parametrize(
    ("unreadable", "mode", "expected"),
    [
        pytest.param(
            "scripts",
            0,
            "Skill 'secret-skill' may be incomplete: failed to read a directory (Permission denied)",
            id="asset-dir",
        ),
        pytest.param(".", 0o100, "Cannot load skill 'secret-skill': Permission denied", id="skill-dir"),
    ],
)
def test_global_skill_files_report_an_unreadable_directory(skill_roots, chmod, unreadable, mode, expected):
    builtin, _ = skill_roots
    write_skill(builtin, "secret-skill", "x", {"scripts/run.py": "print()"})
    chmod(builtin / "secret-skill" / unreadable, mode)

    assert global_skill_files().errors == [expected]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param("plan/SKILL.md", "Cannot load skill 'plan': Permission denied", id="skill-md"),
        pytest.param(
            "plan/scripts/helper.py",
            "Skill 'plan' may be incomplete: failed to load 'helper.py' (Permission denied)",
            id="asset",
        ),
    ],
)
def test_global_skill_files_read_reports_an_unreadable_file(skill_roots, chmod, caplog, path, expected):
    builtin, _ = skill_roots
    write_skill(builtin, "plan", "x", {"scripts/helper.py": "print()"})
    chmod(builtin / path, 0)
    skills = global_skill_files()

    with caplog.at_level(logging.WARNING, logger="daiv.skills"):
        assert skills.read(path) is None

    assert skills.errors == [expected]
    assert str(builtin / path) in caplog.text


def test_list_global_skills_keys_by_frontmatter_name(skill_roots):
    _, custom = skill_roots
    (custom / "team-review").mkdir()
    (custom / "team-review" / "SKILL.md").write_text("---\nname: review-team\ndescription: Review as a team\n---\n")

    assert list_global_skills() == {"review-team": "Review as a team"}


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"no frontmatter at all\n", id="no-frontmatter"),
        pytest.param(b"---\nname: [unclosed\n---\n", id="malformed-yaml"),
        pytest.param(b"---\n- a list\n---\n", id="not-a-mapping"),
        pytest.param(b"\xff\xfe---\n", id="not-utf8"),
        pytest.param(b"---\nname: broken\n---\n", id="no-description"),
    ],
)
def test_list_global_skills_leave_out_skills_the_agent_cannot_load(skill_roots, content):
    builtin, custom = skill_roots
    write_skill(builtin, "plan", "Plan work")
    (custom / "broken").mkdir()
    (custom / "broken" / "SKILL.md").write_bytes(content)

    assert list_global_skills() == {"plan": "Plan work"}


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
async def test_record_invocation_warns_on_unclassified_path(caplog):
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
async def test_record_invocation_creates_row():
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
async def test_record_invocation_swallows_db_errors(caplog):
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
async def test_record_invocation_swallows_missing_thread_id(caplog):
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
async def test_record_invocation_swallows_none_config(caplog):
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


@pytest.mark.django_db(transaction=True)
async def test_classify_source_plugin():
    assert (
        await _classify_source("devkit:commit", "/workspace/plugins/devkit/skills/commit/SKILL.md")
        == SkillInvocation.Source.PLUGIN
    )
