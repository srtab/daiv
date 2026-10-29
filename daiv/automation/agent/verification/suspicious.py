from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

TEST_DIRECTORIES = frozenset({"test", "tests", "__tests__", "spec", "specs"})
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py", "*_test.go", "*.test.*", "*.spec.*", "conftest.py")
CONFIG_FILES = frozenset({
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    ".coveragerc",
    "ruff.toml",
    ".ruff.toml",
    ".flake8",
    "mypy.ini",
    ".pylintrc",
    ".golangci.yml",
    ".golangci.yaml",
    ".rubocop.yml",
    ".editorconfig",
    ".gitlab-ci.yml",
    ".pre-commit-config.yaml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    ".daiv.yml",
})
CONFIG_FILE_PATTERNS = ("jest.config.*", "vitest.config.*", ".eslintrc*", "eslint.config.*", ".prettierrc*")
CONFIG_DIRECTORIES = ((".github", "workflows"), (".circleci",))


def _is_suspicious(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    name = parts[-1]
    if TEST_DIRECTORIES.intersection(parts[:-1]):
        return True
    if name in CONFIG_FILES:
        return True
    if any(fnmatchcase(name, pattern) for pattern in (*TEST_FILE_PATTERNS, *CONFIG_FILE_PATTERNS)):
        return True
    return any(
        parts[i : i + len(directory)] == directory
        for directory in CONFIG_DIRECTORIES
        for i in range(len(parts) - len(directory))
    )


def suspicious_paths(paths: Iterable[str]) -> list[str]:
    """The paths, in order, that are test, lint, CI or DAIV configuration files."""
    return [path for path in paths if _is_suspicious(path)]
