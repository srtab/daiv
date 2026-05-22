from __future__ import annotations

import io
import zipfile
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def build_skill_zip() -> Callable[..., bytes]:
    """Return a helper that builds a skill zip in-memory.

    Default produces a valid zip:
        <skill_name>/SKILL.md

    ``top_level_overrides`` lets a test produce a zip with multiple top-level
    directories. ``omit_skill_md`` skips writing SKILL.md (for testing the
    missing-manifest path). ``extra_files`` adds files under the top-level dir.
    """

    def _build(
        *,
        skill_name: str = "demo",
        description: str = "A demo skill",
        skill_md_extra_body: str = "Body content here.",
        extra_files: dict[str, bytes] | None = None,
        omit_skill_md: bool = False,
        top_level_overrides: list[str] | None = None,
    ) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            names = top_level_overrides or [skill_name]
            for name in names:
                if not omit_skill_md:
                    skill_md = f"---\nname: {name}\ndescription: {description}\n---\n\n{skill_md_extra_body}\n"
                    zf.writestr(f"{name}/SKILL.md", skill_md.encode("utf-8"))
                for rel, data in (extra_files or {}).items():
                    zf.writestr(f"{name}/{rel}", data)
        return buf.getvalue()

    return _build
