from datetime import timedelta

import pytest
from memory.models import (
    EntryStatus,
    MemoryEntry,
    MemoryObservation,
    ObservationCategory,
    ObservationStatus,
    RepositoryMemory,
)
from sessions.models import Run, RunStatus, Session, SessionOrigin


@pytest.mark.django_db
def test_observation_defaults_to_pending_and_survives_run_deletion():
    session = Session.objects.create(thread_id="t1", origin=SessionOrigin.API_JOB, repo_id="group/project")
    run = Run.objects.create(
        session=session, trigger_type=SessionOrigin.API_JOB, repo_id="group/project", status=RunStatus.SUCCESSFUL
    )
    obs = MemoryObservation.objects.create(
        repo_id="group/project",
        run=run,
        category=ObservationCategory.BUILD_TEST,
        content="`make test` requires LANGCHAIN_TRACING_V2=false",
    )
    assert obs.status == ObservationStatus.PENDING

    run.delete()
    obs.refresh_from_db()
    assert obs.run is None, "FK must be SET_NULL so observations outlive run retention"


@pytest.mark.django_db
def test_repository_memory_is_unique_per_repo():
    RepositoryMemory.objects.create(repo_id="group/project", content="## Build & test\n- foo")
    with pytest.raises(Exception, match="(?i)unique|duplicate"):
        RepositoryMemory.objects.create(repo_id="group/project")


def _entry(content, **kwargs):
    return MemoryEntry.objects.create(
        repo_id=kwargs.pop("repo_id", "group/project"),
        category=kwargs.pop("category", ObservationCategory.PITFALL),
        content=content,
        **kwargs,
    )


@pytest.mark.django_db
class TestMemoryEntry:
    def test_new_entry_is_active_and_confirmed_at_creation(self):
        entry = _entry("sandbox timeout is 600s")
        assert entry.status == EntryStatus.ACTIVE
        assert entry.superseded_by is None
        assert entry.last_confirmed_at - entry.created_at < timedelta(seconds=1)

    def test_supersede_retires_the_old_entry_without_touching_its_content(self):
        old = _entry("settings live in daiv/settings/components/")
        new = _entry("settings live in daiv/daiv/settings/components/")
        old.supersede(new)

        old.refresh_from_db()
        assert old.status == EntryStatus.SUPERSEDED
        assert old.superseded_by_id == new.pk
        assert old.content == "settings live in daiv/settings/components/", "history must stay verbatim"
        assert new.status == EntryStatus.ACTIVE

    def test_supersede_without_successor_is_an_eviction(self):
        # Budget pruning retires an entry with nothing replacing it.
        entry = _entry("one-off detail")
        entry.supersede()

        entry.refresh_from_db()
        assert entry.status == EntryStatus.SUPERSEDED
        assert entry.superseded_by is None

    def test_active_queryset_excludes_superseded(self):
        kept = _entry("keep me")
        gone = _entry("retire me")
        gone.supersede()

        assert [e.pk for e in MemoryEntry.objects.active()] == [kept.pk]

    def test_supersede_chain_is_walkable_from_any_historical_version(self):
        v1 = _entry("v1")
        v2 = _entry("v2")
        v3 = _entry("v3")
        v1.supersede(v2)
        v2.supersede(v3)

        chain, cursor = ["v1"], MemoryEntry.objects.get(pk=v1.pk)
        while cursor.superseded_by is not None:
            cursor = cursor.superseded_by
            chain.append(cursor.content)
        assert chain == ["v1", "v2", "v3"]
        assert cursor.status == EntryStatus.ACTIVE

    def test_confirm_only_moves_the_timestamp(self):
        entry = _entry("`make test` needs -n auto")
        later = entry.created_at + timedelta(days=1)
        entry.confirm(later)

        entry.refresh_from_db()
        assert entry.last_confirmed_at == later
        assert entry.content == "`make test` needs -n auto"
        assert entry.status == EntryStatus.ACTIVE

    def test_observations_are_kept_as_provenance(self):
        obs = MemoryObservation.objects.create(
            repo_id="group/project", category=ObservationCategory.PITFALL, content="editing pyproject.toml by hand"
        )
        entry = _entry("never edit pyproject.toml by hand")
        entry.observations.add(obs)

        assert list(entry.observations.all()) == [obs]
        assert list(obs.entries.all()) == [entry]


def test_observation_category_literal_matches_model_choices():
    # The LLM-output Literal (schemas) and the DB TextChoices (models) are declared independently;
    # this guards against silent drift (e.g. adding a category to one but not the other).
    from typing import get_args

    from memory.schemas import ObservationCategoryLiteral

    assert set(get_args(ObservationCategoryLiteral)) == set(ObservationCategory.values)


def test_extraction_prompt_documents_every_category():
    # The extraction prompt hand-lists the categories it may return. A category the model is never
    # told about is never filed under, and the failure is invisible — its section just never renders.
    from memory.prompts import extraction_system

    prompt = extraction_system.prompt.template
    assert [category for category in ObservationCategory.values if f"- {category}:" not in prompt] == []
