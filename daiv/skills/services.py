from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from dataclasses import dataclass
from typing import BinaryIO

import yaml

from skills.constants import (
    ALLOWED_SUFFIXES,
    FORBIDDEN_PATH_PARTS,
    FORBIDDEN_SUFFIXES,
    FRONTMATTER_RE,
    MAX_FILES,
    MAX_PATH_DEPTH,
    MAX_PER_FILE_BYTES,
    MAX_UNPACKED_BYTES,
    MAX_ZIP_BYTES,
    SKILL_NAME_RE,
)

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
            if len(infos) > MAX_FILES:
                raise SkillValidationError(f"zip has too many files (max {MAX_FILES})", code="too_many_files")

            total = 0
            for info in infos:
                cls._check_entry(info)
                total += info.file_size
                if total > MAX_UNPACKED_BYTES:
                    raise SkillValidationError(
                        f"unpacked total exceeds {MAX_UNPACKED_BYTES} bytes", code="unpacked_too_large"
                    )

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

            return cls(
                name=top_level,
                description=str(frontmatter["description"]),
                zip_bytes=zip_bytes,
                zip_size_bytes=len(zip_bytes),
                unpacked_size_bytes=total,
                file_count=len(infos),
                checksum=checksum,
            )

    @staticmethod
    def _check_entry(info: zipfile.ZipInfo) -> None:
        # Symlink check on unix-mode bits in external_attr (high 16 bits)
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise SkillValidationError(f"symlinks are not permitted: {info.filename}", code="symlink")

        name = info.filename
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
            raise SkillValidationError(f"absolute paths are not permitted: {name}", code="unsafe_path")

        parts = name.split("/")
        if ".." in parts:
            raise SkillValidationError(f"path traversal is not permitted: {name}", code="unsafe_path")

        for part in parts:
            if part in FORBIDDEN_PATH_PARTS:
                raise SkillValidationError(
                    f"path contains forbidden segment '{part}': {name}", code="forbidden_path_part"
                )

        leaf = parts[-1]
        suffix = ""
        if "." in leaf:
            suffix = "." + leaf.rsplit(".", 1)[1].lower()

        if suffix in FORBIDDEN_SUFFIXES:
            raise SkillValidationError(f"file type not permitted: {name}", code="disallowed_suffix")
        if suffix not in ALLOWED_SUFFIXES:
            raise SkillValidationError(f"file type not permitted: {name}", code="disallowed_suffix")

        # Depth = number of "/" in the path. "demo/foo.md" has depth 1.
        if name.count("/") > MAX_PATH_DEPTH:
            raise SkillValidationError(f"path is nested too deeply: {name}", code="path_too_deep")

        if info.file_size > MAX_PER_FILE_BYTES:
            raise SkillValidationError(f"file '{name}' exceeds {MAX_PER_FILE_BYTES} bytes", code="per_file_too_large")

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
