from __future__ import annotations

import io
import os
import time
from unittest.mock import patch

import pytest
from skills.constants import TRASH_TTL_SECONDS
from skills.models import GlobalSkill
from skills.services import SkillPackage, SkillStorage, SkillStorageError


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


@pytest.mark.django_db
def test_replace_rolls_back_on_filesystem_error(storage, admin_user, build_skill_zip):
    """If a filesystem write fails mid-swap, the previous tree, zip and DB row must be restored."""
    data_v1 = build_skill_zip(skill_name="demo", description="v1")
    storage.replace(SkillPackage.inspect(io.BytesIO(data_v1)), uploaded_by=admin_user)

    data_v2 = build_skill_zip(skill_name="demo", description="v2")
    pkg_v2 = SkillPackage.inspect(io.BytesIO(data_v2))

    real_write_bytes = type(storage.root).write_bytes
    triggered = []

    def fail_for_new_zip(self, data, *args, **kwargs):
        if self.name == "demo.zip.tmp":
            triggered.append(self)
            raise OSError("simulated disk full")
        return real_write_bytes(self, data, *args, **kwargs)

    with patch.object(type(storage.root), "write_bytes", fail_for_new_zip), pytest.raises(OSError, match="simulated"):
        storage.replace(pkg_v2, uploaded_by=admin_user)

    # Guard against a future rename of the tmp zip suffix silently skipping the failure path.
    assert triggered, "patched write_bytes was never called for demo.zip.tmp"

    base = storage.root
    # Old tree and old zip restored to their canonical locations.
    assert b"v1" in (base / "demo" / "SKILL.md").read_bytes()
    assert (base / ".zips" / "demo.zip").read_bytes() == data_v1
    # DB row unchanged (description still v1).
    assert GlobalSkill.objects.get(name="demo").description == "v1"


@pytest.mark.django_db
def test_replace_rolls_back_on_db_error(storage, admin_user, build_skill_zip):
    data_v1 = build_skill_zip(skill_name="demo", description="v1")
    storage.replace(SkillPackage.inspect(io.BytesIO(data_v1)), uploaded_by=admin_user)

    data_v2 = build_skill_zip(skill_name="demo", description="v2")
    pkg_v2 = SkillPackage.inspect(io.BytesIO(data_v2))

    boom = RuntimeError("simulated db failure")
    with (
        patch("skills.services.GlobalSkill.objects.update_or_create", side_effect=boom),
        pytest.raises(RuntimeError, match="simulated"),
    ):
        storage.replace(pkg_v2, uploaded_by=admin_user)

    base = storage.root
    # Old tree and old zip restored
    assert b"v1" in (base / "demo" / "SKILL.md").read_bytes()
    assert (base / ".zips" / "demo.zip").read_bytes() == data_v1
    # DB row unchanged
    assert GlobalSkill.objects.get(name="demo").description == "v1"


@pytest.mark.django_db
def test_trash_sweep_removes_entries_older_than_ttl(storage):
    base = storage.root
    # Seed an old trashed dir and zip
    old_dir = base / ".trash" / "ancient.1"
    old_dir.mkdir()
    (old_dir / "x.txt").write_text("x")
    old_zip = base / ".trash" / ".zips" / "ancient.1.zip"
    old_zip.write_bytes(b"x")
    ancient = time.time() - (TRASH_TTL_SECONDS + 60)
    os.utime(old_dir, (ancient, ancient))
    os.utime(old_zip, (ancient, ancient))

    storage.sweep_trash()

    assert not old_dir.exists()
    assert not old_zip.exists()


@pytest.mark.django_db
def test_delete_moves_tree_and_zip_to_trash_and_removes_row(storage, admin_user, build_skill_zip):
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)

    storage.delete("demo")

    base = storage.root
    assert not (base / "demo").exists()
    assert not (base / ".zips" / "demo.zip").exists()
    trashed_trees = list((base / ".trash").iterdir())
    trashed_zips = list((base / ".trash" / ".zips").iterdir())
    assert any(p.is_dir() and p.name.startswith("demo.") for p in trashed_trees)
    assert any(p.name.startswith("demo.") for p in trashed_zips)
    assert not GlobalSkill.objects.filter(name="demo").exists()


@pytest.mark.django_db
def test_delete_refuses_builtin_name(storage, monkeypatch):
    monkeypatch.setattr("skills.services.BUILTIN_SKILL_NAMES", frozenset({"plan", "init"}))
    with pytest.raises(SkillStorageError, match="built-in"):
        storage.delete("plan")


@pytest.mark.django_db
def test_replace_refuses_builtin_name(storage, admin_user, build_skill_zip, monkeypatch):
    """Uploads must not be allowed to shadow a built-in skill (it would become un-deletable)."""
    monkeypatch.setattr("skills.services.BUILTIN_SKILL_NAMES", frozenset({"demo"}))
    data = build_skill_zip(skill_name="demo")
    pkg = SkillPackage.inspect(io.BytesIO(data))
    with pytest.raises(SkillStorageError, match="built-in"):
        storage.replace(pkg, uploaded_by=admin_user)
    # No tree, no zip, no DB row left behind.
    assert not (storage.root / "demo").exists()
    assert not (storage.root / ".zips" / "demo.zip").exists()
    assert not GlobalSkill.objects.filter(name="demo").exists()


@pytest.mark.django_db
def test_replace_invalidates_skill_cache(storage, admin_user, tmp_path, build_skill_zip):
    import skills.services as svc_mod

    cache_path = svc_mod.SKILLS_CACHE_PATH

    # Seed a stale cache entry for the skill we're about to upload
    stale_dir = cache_path / "demo"
    stale_dir.mkdir(parents=True)
    (stale_dir / "SKILL.md").write_bytes(b"stale")
    assert (cache_path / "demo" / "SKILL.md").exists()

    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)

    # Cache wiped for this skill
    assert not (cache_path / "demo").exists()
