from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from dataclasses import dataclass
from typing import BinaryIO

import yaml

from skills.constants import FRONTMATTER_RE, MAX_ZIP_BYTES, SKILL_NAME_RE

logger = logging.getLogger("daiv.skills")


class SkillValidationError(Exception):
    """Raised when an uploaded zip fails validation."""

    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class SkillStorageError(Exception):
    """Raised when storing or removing a skill on disk fails."""


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """The validated contents of an uploaded skill zip.

    Constructed only by ``SkillPackage.inspect``. Holds the original zip bytes
    so the storage layer can write the ``.zips/<name>.zip`` sidecar without
    re-reading from the upload stream.
    """

    name: str
    description: str
    zip_bytes: bytes
    zip_size_bytes: int
    unpacked_size_bytes: int
    file_count: int
    checksum: str

    @classmethod
    def inspect(cls, stream: BinaryIO) -> SkillPackage:
        zip_bytes = stream.read()
        if len(zip_bytes) > MAX_ZIP_BYTES:
            raise SkillValidationError(f"zip exceeds maximum size of {MAX_ZIP_BYTES} bytes", code="zip_too_large")
        checksum = hashlib.sha256(zip_bytes).hexdigest()

        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as err:
            raise SkillValidationError("not a valid zip archive", code="bad_zip") from err

        with zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            top_level = cls._single_top_level(infos)
            if not SKILL_NAME_RE.match(top_level):
                raise SkillValidationError(
                    f"top-level directory name '{top_level}' is not a valid skill slug", code="bad_skill_name"
                )

            skill_md_path = f"{top_level}/SKILL.md"
            try:
                skill_md_bytes = zf.read(skill_md_path)
            except KeyError as err:
                raise SkillValidationError(f"missing {skill_md_path}", code="missing_skill_md") from err

            frontmatter = cls._parse_frontmatter(skill_md_bytes.decode("utf-8"))
            cls._require_frontmatter_fields(frontmatter, top_level)

            unpacked = sum(i.file_size for i in infos)
            return cls(
                name=top_level,
                description=str(frontmatter["description"]),
                zip_bytes=zip_bytes,
                zip_size_bytes=len(zip_bytes),
                unpacked_size_bytes=unpacked,
                file_count=len(infos),
                checksum=checksum,
            )

    @staticmethod
    def _single_top_level(infos: list[zipfile.ZipInfo]) -> str:
        roots: set[str] = set()
        for info in infos:
            head, sep, _ = info.filename.partition("/")
            if not sep:
                raise SkillValidationError(
                    "zip contains an entry at the root with no enclosing directory", code="no_top_level_dir"
                )
            roots.add(head)
        if len(roots) != 1:
            raise SkillValidationError(
                "zip must contain exactly one top-level directory", code="multiple_top_level_dirs"
            )
        return next(iter(roots))

    @staticmethod
    def _parse_frontmatter(content: str) -> dict:
        match = FRONTMATTER_RE.match(content)
        if not match:
            raise SkillValidationError("SKILL.md has no YAML frontmatter", code="missing_frontmatter")
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError as err:
            raise SkillValidationError(
                f"SKILL.md frontmatter is not valid YAML: {err}", code="bad_frontmatter_yaml"
            ) from err
        if not isinstance(data, dict):
            raise SkillValidationError("SKILL.md frontmatter must be a mapping", code="bad_frontmatter_type")
        return data

    @staticmethod
    def _require_frontmatter_fields(data: dict, top_level: str) -> None:
        raw_name = data.get("name")
        raw_description = data.get("description")
        name = "" if raw_name is None else str(raw_name).strip()
        description = "" if raw_description is None else str(raw_description).strip()
        if not name:
            raise SkillValidationError("SKILL.md frontmatter is missing 'name'", code="missing_name")
        if name != top_level:
            raise SkillValidationError(
                f"SKILL.md frontmatter name '{name}' does not match top-level directory '{top_level}'",
                code="name_mismatch",
            )
        if not description:
            raise SkillValidationError("SKILL.md frontmatter is missing 'description'", code="missing_description")
        if len(description) > 1024:
            raise SkillValidationError(
                "SKILL.md frontmatter 'description' exceeds 1024 characters", code="description_too_long"
            )
