from datetime import timedelta

from django.db import migrations

# Headers the model-written documents were prompted to emit, mapped to the category they became.
# Must stay in sync with ``memory.tasks.CATEGORY_SECTIONS`` for a document to round-trip.
LEGACY_SECTIONS = {
    "## Build & test": "build_test",
    "## Codebase facts": "codebase_fact",
    "## Pitfalls": "pitfall",
    "## Reviewer preferences": "reviewer_preference",
    "## Workflow": "workflow",
}


def _parse_document(content):
    """Best-effort split of a legacy document into ``(category, content)`` pairs."""
    parsed = []
    category = None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            category = LEGACY_SECTIONS.get(line)
        elif line.startswith(("- ", "* ")) and category:
            parsed.append((category, line[2:].strip()))
        elif parsed and category:
            # A wrapped bullet: fold the continuation back into the entry it belongs to.
            previous_category, previous_content = parsed[-1]
            parsed[-1] = (previous_category, f"{previous_content} {line}")
    return [(category, " ".join(text.split())) for category, text in parsed if text.strip()]


def backfill(apps, schema_editor):
    RepositoryMemory = apps.get_model("memory", "RepositoryMemory")
    MemoryEntry = apps.get_model("memory", "MemoryEntry")

    already_backfilled = set(MemoryEntry.objects.values_list("repo_id", flat=True).distinct())
    for memory in RepositoryMemory.objects.exclude(content="").iterator():
        if memory.repo_id in already_backfilled:
            continue
        if not (parsed := _parse_document(memory.content)):
            continue
        stamp = memory.last_consolidated_at or memory.updated_at
        MemoryEntry.objects.bulk_create([
            MemoryEntry(
                repo_id=memory.repo_id,
                category=category,
                content=content,
                status="active",
                # Distinct, increasing stamps preserve the legacy document's ordering, which
                # ``render_memory_document`` reproduces via its (created_at, pk) sort.
                created_at=stamp + timedelta(microseconds=offset),
                last_confirmed_at=stamp + timedelta(microseconds=offset),
            )
            for offset, (category, content) in enumerate(parsed)
        ])


class Migration(migrations.Migration):
    dependencies = [("memory", "0003_memoryentry")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop, elidable=True)]
