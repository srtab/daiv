"""Repository-wide documentation cross-reference checks.

Lives at the top of tests/unit_tests/ rather than mirroring an app package: it
covers the repository — README, CONTRIBUTING, docs/, and doc URLs embedded in
source — not a daiv module.

Anchors are resolved by instantiating the site's real renderer, so this module needs
the `docs` dependency group. It is in `tool.uv.default-groups`, which is why a plain
`uv sync` installs it; dropping it from there would make these tests fail to import.
"""

from __future__ import annotations

import os
import re
import subprocess  # noqa: S404
from collections import Counter, deque
from functools import cache, partial
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import markdown
from mkdocs.config import load_config

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"

# mike republishes `dev` on every push to main but moves `latest` only on tag pushes, so the
# two aliases serve different trees and each must be resolved against its own.
TREE_VERSION = "dev"
RELEASED_VERSION = "latest"

GENERATED_PAGES = {"llms.txt", "llms-full.txt"}

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
# Untracked, developer-local scratch: absent in CI, present after a local superpowers run.
SKIPPED_PATHS = {DOCS_DIR / "superpowers"}

DOCS_URL_RE = re.compile(r"https://srtab\.github\.io/daiv(?P<rest>/[^\s\"'<>)\]]*)?")
MD_LINK_RE = re.compile(r"\]\(\s*(?P<target>[^)\s]+?)\s*(?:\s+\"[^\"]*\")?\)")
HTML_LINK_RE = re.compile(r"<a\s[^>]*?href=[\"'](?P<target>[^\"']+)[\"']", re.IGNORECASE)


def read_text(path: Path) -> str | None:
    """Return the file's text, or None if it is not valid UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


@cache
def _renderer() -> markdown.Markdown:
    """The site's own markdown renderer, built from the extension set mkdocs.yml declares."""
    config = load_config(str(REPO_ROOT / "mkdocs.yml"))
    return markdown.Markdown(extensions=config["markdown_extensions"], extension_configs=config["mdx_configs"])


def _toc_ids(tokens: list[dict]) -> set[str]:
    ids = set()
    for token in tokens:
        ids.add(token["id"])
        ids |= _toc_ids(token["children"])
    return ids


def anchors_in(content: str) -> set[str]:
    """Anchor ids a markdown source renders to, as produced by the real toc extension."""
    renderer = _renderer()
    renderer.reset()  # toc ids are per-document; without this they would collide across pages
    renderer.convert(content)
    return _toc_ids(renderer.toc_tokens)


class PageRef(NamedTuple):
    """A resolved docs page: how to name it in a failure, and how to read its source."""

    label: str
    read: Callable[[], str | None]


def page_candidates(url_path: str) -> tuple[str, ...]:
    """The docs/-relative sources a mkdocs directory-style URL path could come from."""
    clean = url_path.strip("/")
    if not clean:
        return ("index.md",)
    return (f"{clean}.md", f"{clean}/index.md")


def tree_page_for(url_path: str) -> PageRef | None:
    """Resolve a URL path against the working tree."""
    for candidate in page_candidates(url_path):
        target = DOCS_DIR / candidate
        if target.is_file():
            return PageRef(str(target.relative_to(REPO_ROOT)), partial(read_text, target))
    return None


def git_output(*args: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout


def released_docs_tree() -> tuple[str, set[str]] | None:
    """The tag `latest` points at, plus its docs/-relative sources. None if tags are unavailable.

    Restricted to the `v*.*.*` pattern that docs.yml publishes from, since no other tag can
    have moved the alias. Sorted by version rather than `git describe`, which needs history a
    shallow CI clone lacks.
    """
    tags = (git_output("tag", "--sort=-v:refname", "--list", "v*.*.*") or "").split()
    if not tags:
        return None
    tag = tags[0]
    listing = git_output("ls-tree", "-r", "--name-only", f"{tag}:docs")
    if listing is None:
        return None
    return tag, {line.strip() for line in listing.splitlines() if line.strip()}


def released_page_for(url_path: str, tag: str, tree: set[str]) -> PageRef | None:
    """Resolve a URL path against the docs sources of an already-released tag."""
    for candidate in page_candidates(url_path):
        if candidate in tree:
            return PageRef(f"docs/{candidate} at {tag}", partial(git_output, "show", f"{tag}:docs/{candidate}"))
    return None


def is_skipped(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT)
    if any(part in SKIPPED_DIR_NAMES for part in rel.parts):
        return True
    return any(skipped in path.parents for skipped in SKIPPED_PATHS)


def iter_repo_files() -> Iterator[Path]:
    """Scannable repository files, pruning skipped trees as the walk descends.

    Pruning matters: an unpruned walk of this repo stats ~52k paths (~50k of them in .venv)
    to keep ~900.
    """
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        root = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIR_NAMES and root / d not in SKIPPED_PATHS]
        for name in filenames:
            if (path := root / name).suffix in SCANNED_SUFFIXES:
                yield path


def test_anchors_in_reproduces_the_rendered_ids():
    """Pin the renderer wiring: the cases below only come out right with the site's own extensions.

    `#` inside a fence is not a heading (superfences), repeated headings gain `_N` (toc),
    emoji shortcodes vanish (pymdownx.emoji), and attr_list ids win over the slug. If
    mkdocs.yml stops loading one of those, this fails instead of silently under-reporting.
    """
    source = """# Getting Started

## Prerequisites

```bash
# Install the deps
uv sync
```

## Prerequisites

## :simple-swarm: Docker Swarm (*Recommended*)

## Custom Heading {#explicit-id}
"""
    assert anchors_in(source) == {
        "getting-started",
        "prerequisites",
        "prerequisites_1",
        "docker-swarm-recommended",
        "explicit-id",
    }


def test_absolute_docs_urls_resolve():
    """Every published docs URL in the repository resolves to a real page and anchor.

    `dev` URLs are checked against the working tree; `latest` URLs against the tag that
    alias actually serves, so a link to an unreleased page fails here rather than in
    production.
    """
    failures: list[str] = []
    scanned = list(iter_repo_files())
    files_by_suffix = Counter(path.suffix for path in scanned)
    pages_checked = 0
    released = released_docs_tree()

    for path in scanned:
        rel = path.relative_to(REPO_ROOT)
        content = read_text(path)
        if content is None:
            failures.append(f"{rel}: not valid UTF-8, so it cannot be scanned for docs URLs")
            continue
        for match in DOCS_URL_RE.finditer(content):
            rest = match.group("rest")
            if rest is None:
                continue  # bare project URL, e.g. the OpenRouter HTTP-Referer header
            rest = rest.rstrip(".,;:!?")  # a URL ending a prose sentence
            if not rest.strip("/"):
                continue
            url = match.group(0)
            url_path, _, fragment = rest.partition("#")
            segments = url_path.strip("/").split("/")
            version, page_path = segments[0], "/".join(segments[1:])
            if page_path in GENERATED_PAGES:
                continue

            if version == TREE_VERSION:
                page = tree_page_for(page_path)
                unresolved = f"{rel}: no docs page for {url}"
            elif version == RELEASED_VERSION:
                if released is None:
                    failures.append(f"{rel}: cannot verify {url} — no git tags available to resolve the alias")
                    continue
                tag, tree = released
                page = released_page_for(page_path, tag, tree)
                unresolved = f"{rel}: {url} does not exist at {tag}, which is what '{RELEASED_VERSION}' serves"
            else:
                failures.append(f"{rel}: pins '{version}', expected '{TREE_VERSION}' or '{RELEASED_VERSION}' -> {url}")
                continue

            if page is None:
                failures.append(unresolved)
                continue
            pages_checked += 1
            if not fragment:
                continue
            page_content = page.read()
            if page_content is None:
                failures.append(f"{rel}: could not read {page.label} to verify '#{fragment}'")
            elif fragment not in anchors_in(page_content):
                failures.append(f"{rel}: anchor '#{fragment}' missing from {page.label}")

    # Real breakage is reported before the floors, which would otherwise mask it.
    assert not failures, "Broken documentation URLs:\n" + "\n".join(failures)
    # Floors guard the scanner, not the prose: they must not move when links are added or removed.
    assert len(scanned) > 500, f"only {len(scanned)} files scanned — SCANNED_SUFFIXES or the skip lists drifted"
    for suffix, floor in ((".py", 300), (".html", 50), (".md", 20)):
        assert files_by_suffix[suffix] > floor, (
            f"only {files_by_suffix[suffix]} '{suffix}' files scanned (expected > {floor}) — "
            f"a whole file class dropped out: {dict(files_by_suffix)}"
        )
    assert pages_checked > 10, f"only {pages_checked} docs URLs resolved — DOCS_URL_RE may no longer match"


def test_all_docs_pages_reachable():
    """Every docs page is reachable from the site root by following links.

    Nav membership deliberately does not count: a page reachable only through the
    sidebar is invisible to llms.txt consumers and search-landing readers, which is
    the case this guards. Pages that link only to each other are unreachable even
    though each has an inbound link.
    """
    pages = [p for p in DOCS_DIR.rglob("*.md") if not is_skipped(p)]
    failures: list[str] = []
    outbound: dict[Path, set[Path]] = {}

    for page in pages:
        content = read_text(page)
        if content is None:
            failures.append(f"{page.relative_to(REPO_ROOT)}: not valid UTF-8, so its links cannot be read")
            outbound[page.resolve()] = set()
            continue
        targets = set()
        for match in (*MD_LINK_RE.finditer(content), *HTML_LINK_RE.finditer(content)):
            target = match.group("target").split("#")[0]
            if not target or target.startswith(("http://", "https://", "mailto:", "/")):
                continue
            if not target.endswith(".md"):
                continue
            targets.add((page.parent / target).resolve())
        outbound[page.resolve()] = targets

    root = (DOCS_DIR / "index.md").resolve()
    reached = {root}
    queue = deque([root])
    while queue:
        for target in outbound.get(queue.popleft(), ()):
            if target not in reached:
                reached.add(target)
                queue.append(target)

    unreachable = sorted(str(p.relative_to(REPO_ROOT)) for p in pages if p.resolve() not in reached)
    assert len(pages) > 20, (
        f"Only {len(pages)} docs pages collected — DOCS_DIR or the skip lists may have drifted "
        f"(docs directory moved, or skip logic now matches everything)"
    )
    assert not failures, "Unreadable docs pages:\n" + "\n".join(failures)
    assert not unreachable, "Docs pages not reachable from docs/index.md by following links:\n" + "\n".join(unreachable)
