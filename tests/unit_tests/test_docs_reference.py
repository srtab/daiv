"""The reference pages claim to be complete; hold them to the code.

Like test_env_variables_doc.py, this covers docs pages against the codebase rather than mirroring a daiv module.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from codebase.repo_config import RepositoryConfig
from core.models import SiteConfiguration

DOCS = Path(__file__).resolve().parents[2] / "docs"
SITE_CONFIGURATION_PAGE = DOCS / "reference" / "site-configuration.md"
REPOSITORY_CONFIG_PAGE = DOCS / "customization" / "repository-config.md"


def _leaf_keys(model: type[BaseModel], prefix: str = "") -> list[tuple[str, ...]]:
    keys: list[tuple[str, ...]] = []
    for name, field in model.model_fields.items():
        nested = field.annotation
        if isinstance(nested, type) and issubclass(nested, BaseModel):
            keys.extend(_leaf_keys(nested, f"{prefix}{name}."))
        else:
            keys.append(tuple(f"{prefix}{name}".split(".")))
    return keys


def _table_rows(page: str) -> list[list[str]]:
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in page.splitlines()
        if line.startswith("|")
    ]


def test_site_configuration_lists_every_group():
    rows = _table_rows(SITE_CONFIGURATION_PAGE.read_text(encoding="utf-8"))
    by_title = {row[0]: row for row in rows if len(row) > 1}
    missing = []
    for group in SiteConfiguration.FIELD_GROUPS:
        row = by_title.get(f"**{group.title}**")
        if row is None or row[1] != group.category:
            missing.append(f"{group.title} ({group.category})")
    assert not missing, f"groups missing from the groups table of {SITE_CONFIGURATION_PAGE.name}: {missing}"


def test_repository_config_lists_every_key():
    rows = _table_rows(REPOSITORY_CONFIG_PAGE.read_text(encoding="utf-8"))
    flat = {cell.strip("`") for row in rows for cell in row[:1] if cell.startswith("`")}
    flat = {re.sub(r"\.<name>$", "", key) for key in flat}
    pairs = {
        (row[0].strip("`"), row[1].strip("`"))
        for row in rows
        if len(row) > 1 and row[0].startswith("`") and row[1].startswith("`")
    }
    missing = []
    for parts in _leaf_keys(RepositoryConfig):
        key = ".".join(parts)
        if key in flat or (len(parts) == 2 and parts in pairs):
            continue
        missing.append(key)
    assert not missing, f"keys missing from {REPOSITORY_CONFIG_PAGE.name}: {missing}"
