"""Repository-wide documentation cross-reference checks.

Lives at the top of tests/unit_tests/ rather than mirroring an app package: it
covers the repository — README, CONTRIBUTING, docs/, and doc URLs embedded in
source — not a daiv module.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"

ALLOWED_VERSION = "latest"

GENERATED_PAGES = {"llms.txt"}

SCANNED_SUFFIXES = {".md", ".py", ".html", ".txt", ".yml", ".yaml"}
SKIPPED_DIR_NAMES = {
    ".git",
    ".venv",
    ".ruff_cache",
    ".pytest_cache",
    ".claude",
    ".superpowers",
    "__pycache__",
    "node_modules",
    "site",
    "htmlcov",
}
SKIPPED_PATHS = {DOCS_DIR / "superpowers"}

DOCS_URL_RE = re.compile(r"https://srtab\.github\.io/daiv(?P<rest>/[^\s\"'<>)\]]*)?")
MD_LINK_RE = re.compile(r"\]\(\s*(?P<target>[^)\s]+?)\s*(?:\s+\"[^\"]*\")?\)")
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$", re.MULTILINE)


def slugify(text: str) -> str:
    """Reproduce python-markdown's default TOC slugify."""
    value = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def heading_text(raw: str) -> str:
    """Strip inline markdown so a heading slugifies like its rendered text."""
    text = re.sub(r":[a-z0-9_+-]+:", "", raw)  # pymdownx.emoji shortcodes render to nothing in the TOC
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return text.replace("`", "").replace("*", "")


def anchors_of(path: Path) -> set[str]:
    content = path.read_text(encoding="utf-8")
    return {slugify(heading_text(m.group("text"))) for m in HEADING_RE.finditer(content)}


def docs_file_for(url_path: str) -> Path | None:
    """Map a mkdocs directory-style URL path to its source .md file."""
    clean = url_path.strip("/")
    if not clean:
        return DOCS_DIR / "index.md"
    for candidate in (DOCS_DIR / f"{clean}.md", DOCS_DIR / clean / "index.md"):
        if candidate.is_file():
            return candidate
    return None


def is_skipped(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT)
    if any(part in SKIPPED_DIR_NAMES for part in rel.parts):
        return True
    return any(skipped in path.parents for skipped in SKIPPED_PATHS)


def iter_repo_files() -> Iterator[Path]:
    for path in REPO_ROOT.rglob("*"):
        if path.is_file() and path.suffix in SCANNED_SUFFIXES and not is_skipped(path):
            yield path


def test_absolute_docs_urls_resolve():
    failures = []
    checked_count = 0
    for path in iter_repo_files():
        content = path.read_text(encoding="utf-8", errors="ignore")
        for match in DOCS_URL_RE.finditer(content):
            rest = match.group("rest")
            if rest is None or not rest.strip("/"):
                continue  # bare project URL, e.g. the OpenRouter HTTP-Referer header
            checked_count += 1
            url_path, _, fragment = rest.partition("#")
            segments = url_path.strip("/").split("/")
            version, page_segments = segments[0], segments[1:]
            rel = path.relative_to(REPO_ROOT)
            if version != ALLOWED_VERSION:
                failures.append(f"{rel}: pins '{version}', expected '{ALLOWED_VERSION}' -> {match.group(0)}")
                continue
            page_path = "/".join(page_segments)
            if page_path in GENERATED_PAGES:
                continue
            target = docs_file_for(page_path)
            if target is None:
                failures.append(f"{rel}: no docs page for {match.group(0)}")
                continue
            if fragment and fragment not in anchors_of(target):
                failures.append(f"{rel}: anchor '#{fragment}' missing from {target.relative_to(REPO_ROOT)}")
    assert checked_count > 20, (
        f"Only {checked_count} docs URLs evaluated — DOCS_URL_RE or SKIPPED_DIR_NAMES may have drifted "
        f"(base URL changed, or skip logic now matches everything)"
    )
    assert not failures, "Broken documentation URLs:\n" + "\n".join(failures)


def test_no_orphan_docs_pages():
    """Every docs page needs at least one inbound link.

    Only the site root is exempt. Section index pages such as
    integrations/rt/index.md are not — they are exactly the kind of page
    that goes orphaned.
    """
    pages = [p for p in DOCS_DIR.rglob("*.md") if not is_skipped(p)]
    linked: set[Path] = set()
    for page in pages:
        for match in MD_LINK_RE.finditer(page.read_text(encoding="utf-8")):
            target = match.group("target").split("#")[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not target.endswith(".md"):
                continue
            resolved = (page.parent / target).resolve()
            if resolved != page.resolve():
                linked.add(resolved)

    root_index = (DOCS_DIR / "index.md").resolve()
    orphans = sorted(
        str(p.relative_to(REPO_ROOT)) for p in pages if p.resolve() != root_index and p.resolve() not in linked
    )
    assert len(pages) > 20, (
        f"Only {len(pages)} docs pages collected — DOCS_DIR or SKIPPED_DIR_NAMES may have drifted "
        f"(docs directory moved, or skip logic now matches everything)"
    )
    assert not orphans, "Docs pages with no inbound link:\n" + "\n".join(orphans)
