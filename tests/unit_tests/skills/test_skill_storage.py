from __future__ import annotations

import io

import pytest
from skills.models import GlobalSkill
from skills.services import SkillPackage, SkillStorage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Point CUSTOM_SKILLS_PATH and SKILLS_CACHE_PATH at temp dirs and
    return a SkillStorage instance bound to those paths."""
    custom = tmp_path / "custom"
    cache = tmp_path / "cache"
    custom.mkdir()
    cache.mkdir()
    monkeypatch.setattr("skills.services.agent_settings.CUSTOM_SKILLS_PATH", custom)
    monkeypatch.setattr("skills.services.SKILLS_CACHE_PATH", cache)
    return SkillStorage()


@pytest.mark.django_db
def test_replace_writes_unpacked_tree_zip_and_row(storage, admin_user, tmp_path, build_skill_zip):
    data = build_skill_zip(skill_name="demo", extra_files={"scripts/foo.py": b"print('hi')\n"})
    pkg = SkillPackage.inspect(io.BytesIO(data))

    storage.replace(pkg, uploaded_by=admin_user)

    base = storage.root
    assert (base / "demo" / "SKILL.md").exists()
    assert (base / "demo" / "scripts" / "foo.py").read_bytes() == b"print('hi')\n"
    assert (base / ".zips" / "demo.zip").read_bytes() == data

    skill = GlobalSkill.objects.get(name="demo")
    assert skill.uploaded_by == admin_user
    assert skill.file_count == 2
    assert skill.size_bytes == pkg.unpacked_size_bytes
    assert skill.checksum == pkg.checksum


@pytest.mark.django_db
def test_replace_overwrites_and_moves_old_to_trash(storage, admin_user, build_skill_zip):
    data_v1 = build_skill_zip(skill_name="demo", description="v1")
    storage.replace(SkillPackage.inspect(io.BytesIO(data_v1)), uploaded_by=admin_user)

    data_v2 = build_skill_zip(skill_name="demo", description="v2", extra_files={"scripts/foo.py": b"new\n"})
    storage.replace(SkillPackage.inspect(io.BytesIO(data_v2)), uploaded_by=admin_user)

    base = storage.root
    # New tree present, new zip present
    assert b"v2" in (base / "demo" / "SKILL.md").read_bytes()
    assert (base / "demo" / "scripts" / "foo.py").exists()
    assert (base / ".zips" / "demo.zip").read_bytes() == data_v2

    # Old tree moved to .trash, old zip moved to .trash/.zips
    trashed_trees = list((base / ".trash").iterdir())
    trashed_zips = list((base / ".trash" / ".zips").iterdir())
    # Both lists contain at least one entry whose name starts with "demo."
    assert any(p.is_dir() and p.name.startswith("demo.") for p in trashed_trees)
    assert any(p.suffix == ".zip" and p.name.startswith("demo.") for p in trashed_zips)

    # DB row updated, not duplicated
    assert GlobalSkill.objects.filter(name="demo").count() == 1
    assert GlobalSkill.objects.get(name="demo").description == "v2"
