from __future__ import annotations

import io
import zipfile

import pytest
from skills.constants import MAX_FILES, MAX_PER_FILE_BYTES, MAX_UNPACKED_BYTES
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


def _valid_skill_md() -> tuple[str, bytes]:
    return ("demo/SKILL.md", b"---\nname: demo\ndescription: d\n---\nbody\n")


def test_path_traversal_rejected():
    data = _raw_zip([_valid_skill_md(), ("demo/../escaped.txt", b"x")])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "unsafe_path"


def test_absolute_path_rejected():
    data = _raw_zip([_valid_skill_md(), ("/abs/path.txt", b"x")])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "unsafe_path"


def test_windows_drive_path_rejected():
    data = _raw_zip([_valid_skill_md(), ("C:/x.txt", b"x")])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "unsafe_path"


def test_symlink_rejected():
    # External attrs encode the unix file mode in the high 16 bits.
    # 0o120000 == S_IFLNK.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(*_valid_skill_md())
        info = zipfile.ZipInfo("demo/link")
        info.external_attr = (0o120000 | 0o777) << 16
        zf.writestr(info, b"../target")
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(buf.getvalue()))
    assert exc.value.code == "symlink"


def test_disallowed_suffix_rejected():
    data = _raw_zip([_valid_skill_md(), ("demo/payload.exe", b"x")])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "disallowed_suffix"


def test_forbidden_path_part_rejected():
    data = _raw_zip([_valid_skill_md(), ("demo/__pycache__/foo.pyc", b"x")])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    # FORBIDDEN_PATH_PARTS is checked before suffix, so __pycache__ wins over .pyc
    assert exc.value.code == "forbidden_path_part"


def test_path_too_deep_rejected():
    deep = "demo/" + "/".join(f"d{i}" for i in range(9)) + "/x.md"
    data = _raw_zip([_valid_skill_md(), (deep, b"x")])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "path_too_deep"


def test_per_file_too_large_rejected():
    big = b"a" * (MAX_PER_FILE_BYTES + 1)
    data = _raw_zip([_valid_skill_md(), ("demo/big.txt", big)])
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "per_file_too_large"


def test_too_many_files_rejected():
    entries = [_valid_skill_md()]
    for i in range(MAX_FILES + 1):
        entries.append((f"demo/f{i}.md", b"x"))
    data = _raw_zip(entries)
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(data))
    assert exc.value.code == "too_many_files"


def test_unpacked_total_too_large_rejected():
    # 26 files * 1 MiB each = 26 MiB unpacked, exceeding the 25 MiB cap.
    # Use ZIP_DEFLATED so the zip bytes stay under MAX_ZIP_BYTES while the
    # unpacked total exceeds MAX_UNPACKED_BYTES.
    chunk = b"a" * MAX_PER_FILE_BYTES
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(*_valid_skill_md())
        for i in range(26):
            zf.writestr(f"demo/f{i}.txt", chunk)
    with pytest.raises(SkillValidationError) as exc:
        SkillPackage.inspect(io.BytesIO(buf.getvalue()))
    assert exc.value.code == "unpacked_too_large"
    # Anchor the cap value so a future change here flags a regression
    assert MAX_UNPACKED_BYTES == 25 * 1024 * 1024
