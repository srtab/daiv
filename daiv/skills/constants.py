from __future__ import annotations

import re
import tempfile
from pathlib import Path

BUILTIN_SKILLS_PATH = Path(__file__).resolve().parent.parent / "automation" / "agent" / "skills"

SKILLS_PATH = "/workspace/skills"

# Host directory behind ``SKILLS_PATH`` in disk mode. It can outlive a restart (``/tmp`` need not be a tmpfs), so only
# ``SkillStorage`` invalidates it.
SKILLS_CACHE_PATH = Path(tempfile.gettempdir()) / "daiv-skills"

MAX_ZIP_BYTES = 5 * 1024 * 1024  # 5 MiB, compressed
MAX_UNPACKED_BYTES = 25 * 1024 * 1024  # 25 MiB, sum of ZipInfo.file_size
MAX_FILES = 200
MAX_PATH_DEPTH = 8
MAX_PER_FILE_BYTES = 1 * 1024 * 1024  # 1 MiB

ALLOWED_SUFFIXES: frozenset[str] = frozenset({
    ".md",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".sh",
    ".toml",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".gif",
})

FORBIDDEN_PATH_PARTS: frozenset[str] = frozenset({".git", ".gitignore", "__pycache__"})
FORBIDDEN_SUFFIXES: frozenset[str] = frozenset({".pyc"})

SKILL_NAME_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
FRONTMATTER_RE: re.Pattern[str] = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Subdirectories inside CUSTOM_SKILLS_PATH used by the storage layer. Names
# start with a `.` so the agent's skill walker skips them.
TRASH_DIR = ".trash"
TRASH_ZIPS_DIR = ".trash/.zips"
TMP_DIR = ".tmp"
ZIPS_DIR = ".zips"

# Time after which entries in .trash and .trash/.zips are swept on the next
# upload. Keep symmetrical with the user-facing recovery story.
TRASH_TTL_SECONDS = 24 * 60 * 60
