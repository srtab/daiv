from __future__ import annotations

import io

from skills.services import SkillPackage, SkillValidationError  # noqa: F401


def test_valid_zip_inspects_cleanly(build_skill_zip):
    data = build_skill_zip(skill_name="demo", description="A demo skill")
    pkg = SkillPackage.inspect(io.BytesIO(data))

    assert pkg.name == "demo"
    assert pkg.description == "A demo skill"
    assert pkg.file_count == 1  # just SKILL.md
    assert pkg.unpacked_size_bytes > 0
    assert pkg.zip_size_bytes == len(data)
    assert len(pkg.checksum) == 64  # sha256 hex


def test_valid_zip_with_extra_files(build_skill_zip):
    data = build_skill_zip(
        skill_name="demo",
        description="A demo",
        extra_files={"scripts/foo.py": b"print('hi')\n", "references/x.md": b"# Note\n"},
    )
    pkg = SkillPackage.inspect(io.BytesIO(data))

    assert pkg.file_count == 3
