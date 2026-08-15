from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from memory.constants import MEMORY_MAX_BYTES, MEMORY_MAX_LINES
from memory.models import ObservationCategory

if TYPE_CHECKING:
    from collections.abc import Iterable

    from memory.models import MemoryEntry

# The only categories the document renders. An entry outside them contributes nothing to the
# budget yet is still an eviction candidate, so it gets superseded to free bytes it never used.
CATEGORY_SECTIONS: tuple[tuple[str, str], ...] = (
    (ObservationCategory.BUILD_TEST, "## Build & test"),
    (ObservationCategory.CODEBASE_FACT, "## Codebase facts"),
    (ObservationCategory.PITFALL, "## Pitfalls"),
    (ObservationCategory.REVIEWER_PREFERENCE, "## Reviewer preferences"),
    (ObservationCategory.WORKFLOW, "## Workflow"),
)

# Section order doubles as the eviction tie-break between equally large categories.
_SECTION_ORDER = {category: index for index, (category, _header) in enumerate(CATEGORY_SECTIONS)}


def render_memory_document(entries: Iterable[MemoryEntry]) -> str:
    """Render active entries into the document injected into agent runs.

    Deterministic by construction — fixed section order, entries by creation time then id (a
    round stamps one timestamp on all of its entries), whitespace collapsed so one entry is
    always exactly one line. No model involvement.
    """
    by_category: dict[str, list[MemoryEntry]] = defaultdict(list)
    for entry in entries:
        by_category[entry.category].append(entry)

    sections = []
    for category, header in CATEGORY_SECTIONS:
        if not (items := sorted(by_category.get(category, ()), key=_render_order)):
            continue
        bullets = "\n".join(f"- {' '.join(entry.content.split())}" for entry in items)
        sections.append(f"{header}\n{bullets}")
    return "\n\n".join(sections)


def _render_order(entry: MemoryEntry) -> tuple:
    return (entry.created_at, str(entry.pk))


def _eviction_order(entry: MemoryEntry) -> tuple:
    return (entry.last_confirmed_at, entry.created_at, str(entry.pk))


def document_size(document: str) -> tuple[int, int]:
    """The document's ``(lines, bytes)`` — the two dimensions the render budget is expressed in."""
    return len(document.splitlines()), len(document.encode("utf-8"))


def _fits_budget(document: str, *, max_lines: int, max_bytes: int) -> bool:
    lines, size = document_size(document)
    return lines <= max_lines and size <= max_bytes


def _largest_category(entries: Iterable[MemoryEntry]) -> str:
    content_bytes: Counter[str] = Counter()
    for entry in entries:
        content_bytes[entry.category] += len(entry.content.encode("utf-8"))
    return min(
        content_bytes,
        key=lambda category: (-content_bytes[category], _SECTION_ORDER.get(category, len(_SECTION_ORDER))),
    )


def prune_to_budget(
    entries: Iterable[MemoryEntry], *, max_lines: int = MEMORY_MAX_LINES, max_bytes: int = MEMORY_MAX_BYTES
) -> tuple[list[MemoryEntry], list[MemoryEntry]]:
    """Split entries into the ones that fit the render budget and the ones to evict.

    Pressure-triggered and category-scoped: while the render fits, nothing is evicted; under
    pressure the biggest category gives up its least-recently-confirmed entry, so a small
    category is never drained to make room for a large one.

    The last entry is never evicted. A budget too small to hold even one entry is a
    misconfiguration, and overshooting it by a single bullet beats erasing the repository's
    whole memory.
    """
    kept = list(entries)
    evicted: list[MemoryEntry] = []
    while len(kept) > 1 and not _fits_budget(render_memory_document(kept), max_lines=max_lines, max_bytes=max_bytes):
        category = _largest_category(kept)
        victim = min((entry for entry in kept if entry.category == category), key=_eviction_order)
        kept.remove(victim)
        evicted.append(victim)
    return kept, evicted
