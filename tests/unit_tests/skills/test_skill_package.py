from __future__ import annotations

import io

import pytest
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


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"omit_skill_md": True, "extra_files": {"scripts/foo.py": b"# placeholder\n"}}, "missing_skill_md"),
        ({"top_level_overrides": ["foo", "bar"]}, "multiple_top_level_dirs"),
        ({"skill_name": "UPPER_CASE"}, "bad_skill_name"),
        ({"skill_name": "0-good"}, None),  # leading digit is fine
    ],
)
def test_structural_validation(build_skill_zip, kwargs, code):
    data = build_skill_zip(**kwargs)
    if code is None:
        SkillPackage.inspect(io.BytesIO(data))  # no error
        return
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == code


def _raw_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """Helper local to this module for tests that bypass the normal skill layout."""
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in entries:
            zf.writestr(path, data)
    return buf.getvalue()


def test_frontmatter_missing_block():
    data = _raw_zip([("demo/SKILL.md", b"no frontmatter here\n")])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "missing_frontmatter"


def test_frontmatter_name_mismatch():
    data = _raw_zip([("demo/SKILL.md", b"---\nname: other\ndescription: d\n---\nbody\n")])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "name_mismatch"


def test_frontmatter_missing_description():
    data = _raw_zip([("demo/SKILL.md", b"---\nname: demo\n---\nbody\n")])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "missing_description"


def test_bad_yaml_in_frontmatter():
    data = _raw_zip([("demo/SKILL.md", b"---\n: : not yaml\n---\nbody\n")])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "bad_frontmatter_yaml"


def test_bad_zip_bytes():
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(b"not a zip"))
    assert exc.value.code == "bad_zip"
