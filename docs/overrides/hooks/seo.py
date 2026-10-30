"""MkDocs hooks: search-engine settings for the dev version, llms.txt descriptions from page front matter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mike.mkdocs_utils import docs_version_var
from mkdocs.plugins import event_priority
from mkdocs.utils.meta import get_data

if TYPE_CHECKING:
    from mkdocs.config.defaults import MkDocsConfig

DEV_VERSION = "dev"


# Ahead of mike's on_config, which turns canonical_version into site_url.
@event_priority(100)
def on_config(config: MkDocsConfig) -> None:
    if os.environ.get(docs_version_var) == DEV_VERSION:
        # Self-canonical, not latest: Google can carry a noindex across a canonical to the target page.
        config.plugins["mike"].config["canonical_version"] = DEV_VERSION
        config.extra["noindex"] = True

    llmstxt = config.plugins["llmstxt"].config
    llmstxt["sections"] = {
        section: [_with_description(config.docs_dir, path) for path in paths]
        for section, paths in llmstxt["sections"].items()
    }


def _with_description(docs_dir: str, path: str) -> dict[str, str]:
    _, meta = get_data(Path(docs_dir, path).read_text(encoding="utf-8"))
    return {path: meta.get("description", "")}
