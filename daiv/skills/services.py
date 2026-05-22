from __future__ import annotations

import hashlib
import io
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from django.db import transaction

import yaml

from automation.agent.conf import settings as agent_settings
from automation.agent.constants import SKILLS_CACHE_PATH
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
    TMP_DIR,
    TRASH_DIR,
    TRASH_ZIPS_DIR,
    ZIPS_DIR,
)
from skills.models import GlobalSkill

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


class SkillStorage:
    """Owns the on-disk layout under ``CUSTOM_SKILLS_PATH`` and the DB row for
    each global skill. The agent's skills middleware reads from ``self.root``
    directly; this class is the only thing that writes to it."""

    def __init__(self) -> None:
        if agent_settings.CUSTOM_SKILLS_PATH is None:
            raise SkillStorageError("CUSTOM_SKILLS_PATH is not configured")
        self.root: Path = Path(agent_settings.CUSTOM_SKILLS_PATH)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / TMP_DIR).mkdir(exist_ok=True)
        (self.root / TRASH_DIR).mkdir(exist_ok=True)
        (self.root / TRASH_ZIPS_DIR).mkdir(parents=True, exist_ok=True)
        (self.root / ZIPS_DIR).mkdir(exist_ok=True)

    def replace(self, pkg: SkillPackage, *, uploaded_by) -> GlobalSkill:
        staged = self._stage_extract(pkg)
        try:
            with transaction.atomic():
                skill, _created = GlobalSkill.objects.update_or_create(
                    name=pkg.name,
                    defaults={
                        "description": pkg.description,
                        "uploaded_by": uploaded_by,
                        "size_bytes": pkg.unpacked_size_bytes,
                        "file_count": pkg.file_count,
                        "checksum": pkg.checksum,
                    },
                )
                self._swap_in(pkg, staged)
        except Exception:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        finally:
            self._invalidate_cache(pkg.name)
        return skill

    def _stage_extract(self, pkg: SkillPackage) -> Path:
        staged_parent = self.root / TMP_DIR
        staged = Path(tempfile.mkdtemp(prefix=f"{pkg.name}.", dir=staged_parent))
        with zipfile.ZipFile(io.BytesIO(pkg.zip_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = staged / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, Path(target).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        return staged

    def _swap_in(self, pkg: SkillPackage, staged: Path) -> None:
        new_tree = self.root / pkg.name
        staged_tree = staged / pkg.name
        if not staged_tree.is_dir():
            raise SkillStorageError(f"staged extraction did not produce '{pkg.name}/' top-level dir")
        Path(staged_tree).replace(new_tree)
        new_zip = self.root / ZIPS_DIR / f"{pkg.name}.zip"
        tmp_zip = self.root / ZIPS_DIR / f"{pkg.name}.zip.tmp"
        tmp_zip.write_bytes(pkg.zip_bytes)
        Path(tmp_zip).replace(new_zip)
        shutil.rmtree(staged, ignore_errors=True)

    def _invalidate_cache(self, name: str) -> None:
        # Best-effort: the agent's per-turn check is "file exists on disk in
        # SKILLS_CACHE_PATH → skip upload". Wiping the per-skill subdir means
        # the next turn re-uploads.
        try:
            shutil.rmtree(SKILLS_CACHE_PATH / name, ignore_errors=True)
        except OSError:
            logger.exception("Failed to invalidate skills cache for %r", name)
