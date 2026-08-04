from datetime import timedelta

from django.utils import timezone

import pytest
from memory.models import MemoryEntry, ObservationCategory
from memory.tasks import CATEGORY_SECTIONS, MEMORY_MAX_BYTES, MEMORY_MAX_LINES, prune_to_budget, render_memory_document

NOW = timezone.now()


def _entry(content, category=ObservationCategory.PITFALL, *, created_offset=0, confirmed_offset=0):
    """An unsaved entry — the render and the budget policy are pure functions over entries."""
    created = NOW + timedelta(seconds=created_offset)
    return MemoryEntry(
        repo_id="group/project",
        category=category,
        content=content,
        created_at=created,
        last_confirmed_at=NOW + timedelta(seconds=confirmed_offset),
    )


def test_every_category_has_a_section_to_render_into():
    # render_memory_document iterates CATEGORY_SECTIONS, not the choices: a category missing from
    # it produces entries that are ACTIVE, count against the byte budget, and appear nowhere.
    assert {category for category, _header in CATEGORY_SECTIONS} == set(ObservationCategory.values)


def test_section_order_is_stable_across_equally_sized_categories():
    # _largest_category breaks size ties on CATEGORY_SECTIONS order. Listed against that order on
    # purpose: without the tiebreak, min() returns whichever equal-sized category it saw first.
    same_size = [
        _entry("xxxx", ObservationCategory.WORKFLOW, created_offset=0),
        _entry("xxxx", ObservationCategory.BUILD_TEST, created_offset=1),
    ]

    kept, evicted = prune_to_budget(same_size, max_lines=3, max_bytes=MEMORY_MAX_BYTES)

    assert [entry.category for entry in evicted] == [ObservationCategory.BUILD_TEST]
    assert [entry.category for entry in kept] == [ObservationCategory.WORKFLOW]


class TestRender:
    def test_sections_follow_the_declared_order_and_omit_empty_ones(self):
        document = render_memory_document([
            _entry("a workflow rule", ObservationCategory.WORKFLOW),
            _entry("a build fact", ObservationCategory.BUILD_TEST),
            _entry("a pitfall", ObservationCategory.PITFALL),
        ])

        assert document == (
            "## Build & test\n- a build fact\n\n## Pitfalls\n- a pitfall\n\n## Workflow\n- a workflow rule"
        )

    def test_entries_render_oldest_first_within_a_section(self):
        document = render_memory_document([
            _entry("second", created_offset=2),
            _entry("first", created_offset=1),
            _entry("third", created_offset=3),
        ])

        assert document == "## Pitfalls\n- first\n- second\n- third"

    def test_multiline_content_collapses_to_one_bullet(self):
        # One entry is always exactly one line, so the line budget means what it says.
        document = render_memory_document([_entry("first line\n  second line\ttabbed")])

        assert document == "## Pitfalls\n- first line second line tabbed"

    def test_rerender_is_byte_identical(self):
        entries = [_entry(f"fact {i}", created_offset=i) for i in range(10)]

        assert render_memory_document(entries) == render_memory_document(entries)
        assert render_memory_document(entries) == render_memory_document(list(reversed(entries)))

    def test_no_entries_render_an_empty_document(self):
        assert render_memory_document([]) == ""


class TestPruneToBudget:
    def test_nothing_is_evicted_while_the_render_fits(self):
        entries = [_entry(f"fact {i}", confirmed_offset=-i * 1000) for i in range(20)]

        kept, evicted = prune_to_budget(entries)

        assert evicted == []
        assert kept == entries

    def test_least_recently_confirmed_goes_first_within_the_over_budget_category(self):
        # The pitfall section holds the excess; the workflow entry must survive untouched even
        # though it was confirmed longer ago than any pitfall.
        stale_workflow = _entry("stale but a different category", ObservationCategory.WORKFLOW, confirmed_offset=-9999)
        pitfalls = [_entry(f"pitfall {i} " + "x" * 100, confirmed_offset=-i) for i in range(6)]

        kept, evicted = prune_to_budget([stale_workflow, *pitfalls], max_lines=100, max_bytes=400)

        assert stale_workflow in kept
        assert evicted, "the over-budget category must give something up"
        # Evicted strictly in least-recently-confirmed order, all from the over-budget category.
        assert [entry.content for entry in evicted] == [
            pitfalls[index].content for index in range(5, 5 - len(evicted), -1)
        ]

    def test_line_budget_also_triggers_eviction(self):
        entries = [_entry(f"fact {i}", confirmed_offset=-i) for i in range(10)]

        # 1 header + 10 bullets = 11 lines; cap at 6 lines keeps the header + 5 bullets.
        kept, evicted = prune_to_budget(entries, max_lines=6)

        assert len(kept) == 5
        assert len(evicted) == 5
        assert len(render_memory_document(kept).splitlines()) == 6

    def test_eviction_never_empties_the_document(self):
        # A single entry larger than the whole budget cannot be made to fit; the loop must
        # terminate holding it rather than spinning or erasing the repository's memory.
        only = _entry("x" * 100)
        kept, evicted = prune_to_budget([only], max_bytes=10)

        assert kept == [only]
        assert evicted == []

    def test_a_pathological_budget_keeps_one_entry_instead_of_wiping_memory(self):
        entries = [_entry(f"fact {index}", created_offset=index) for index in range(5)]

        kept, evicted = prune_to_budget(entries, max_lines=0, max_bytes=0)

        assert len(kept) == 1, "a zeroed budget must not supersede every entry the repo has"
        assert len(evicted) == 4


@pytest.mark.django_db
def test_rendered_document_is_the_read_location_agents_use():
    # The read contract: whatever the render produces must be reachable through
    # RepositoryMemory.content, which is what the agent system prompt injects.
    from memory.models import RepositoryMemory

    entries = [MemoryEntry.objects.create(repo_id="group/project", category=ObservationCategory.PITFALL, content="a")]
    memory = RepositoryMemory.objects.create(repo_id="group/project", content=render_memory_document(entries))

    assert memory.content == "## Pitfalls\n- a"


def test_budget_defaults_are_the_documented_caps():
    entries = [_entry("x" * 200, created_offset=i) for i in range(200)]

    kept, evicted = prune_to_budget(entries)

    assert evicted, "the default caps must actually bound the render"
    document = render_memory_document(kept)
    assert len(document.splitlines()) <= MEMORY_MAX_LINES
    assert len(document.encode("utf-8")) <= MEMORY_MAX_BYTES
