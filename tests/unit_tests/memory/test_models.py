import pytest
from memory.models import MemoryObservation, ObservationCategory, ObservationStatus, RepositoryMemory
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


def test_observation_category_literal_matches_model_choices():
    # The LLM-output Literal (schemas) and the DB TextChoices (models) are declared independently;
    # this guards against silent drift (e.g. adding a category to one but not the other).
    from typing import get_args

    from memory.schemas import ObservationCategoryLiteral

    assert set(get_args(ObservationCategoryLiteral)) == set(ObservationCategory.values)
