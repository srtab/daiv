from importlib import import_module

import pytest
from memory.models import MemoryEntry, ObservationCategory, RepositoryMemory
from memory.render import CATEGORY_SECTIONS, render_memory_document

backfill_migration = import_module("memory.migrations.0004_backfill_entries_from_documents")


def test_legacy_headers_cover_every_rendered_section():
    # A header the parser does not know silently drops that section's facts on migration.
    assert set(backfill_migration.LEGACY_SECTIONS) == {header for _category, header in CATEGORY_SECTIONS}
    assert set(backfill_migration.LEGACY_SECTIONS.values()) == set(ObservationCategory.values)


def test_parse_document_round_trips_a_rendered_document():
    entries = [
        MemoryEntry(repo_id="r", category=ObservationCategory.BUILD_TEST, content="make test needs no tracing"),
        MemoryEntry(repo_id="r", category=ObservationCategory.PITFALL, content="settings live in components/"),
        MemoryEntry(repo_id="r", category=ObservationCategory.WORKFLOW, content="branch before committing"),
    ]
    document = render_memory_document(entries)

    assert backfill_migration._parse_document(document) == [
        ("build_test", "make test needs no tracing"),
        ("pitfall", "settings live in components/"),
        ("workflow", "branch before committing"),
    ]


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("", []),
        ("no headers, no bullets", []),
        ("- orphan bullet with no section", []),
        ("## Pitfalls\n- one\n* two", [("pitfall", "one"), ("pitfall", "two")]),
        ("## Unknown section\n- dropped", []),
        ("## Pitfalls\n- a bullet\n  wrapped onto a second line", [("pitfall", "a bullet wrapped onto a second line")]),
        ("## Pitfalls\n-   spaced   out   ", [("pitfall", "spaced out")]),
        ("## Pitfalls\n- \n- real", [("pitfall", "real")]),
    ],
)
def test_parse_document_edge_cases(document, expected):
    assert backfill_migration._parse_document(document) == expected


@pytest.mark.django_db
def test_backfill_creates_ordered_entries_and_skips_repos_that_already_have_them():
    from django.apps import apps

    RepositoryMemory.objects.create(
        repo_id="group/legacy", content="## Pitfalls\n- first fact\n- second fact\n\n## Workflow\n- third fact"
    )
    RepositoryMemory.objects.create(repo_id="group/already-migrated", content="## Pitfalls\n- ignored")
    RepositoryMemory.objects.create(repo_id="group/blank", content="")
    MemoryEntry.objects.create(
        repo_id="group/already-migrated", category=ObservationCategory.PITFALL, content="pre-existing"
    )

    backfill_migration.backfill(apps, None)

    migrated = list(MemoryEntry.objects.filter(repo_id="group/legacy").order_by("created_at"))
    assert [entry.content for entry in migrated] == ["first fact", "second fact", "third fact"]
    assert [entry.category for entry in migrated] == ["pitfall", "pitfall", "workflow"]
    assert render_memory_document(migrated) == "## Pitfalls\n- first fact\n- second fact\n\n## Workflow\n- third fact"

    assert MemoryEntry.objects.filter(repo_id="group/already-migrated").count() == 1
    assert not MemoryEntry.objects.filter(repo_id="group/blank").exists()


@pytest.mark.django_db
def test_backfill_is_idempotent():
    from django.apps import apps

    RepositoryMemory.objects.create(repo_id="group/legacy", content="## Pitfalls\n- only fact")

    backfill_migration.backfill(apps, None)
    backfill_migration.backfill(apps, None)

    assert MemoryEntry.objects.filter(repo_id="group/legacy").count() == 1
