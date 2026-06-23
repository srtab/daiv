import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from memory.models import MemoryObservation, ObservationCategory, ObservationStatus, RepositoryMemory


@pytest.mark.django_db
def test_observation_defaults_to_pending_and_survives_activity_deletion():
    activity = Activity.objects.create(
        trigger_type=TriggerType.API_JOB, repo_id="group/project", status=ActivityStatus.SUCCESSFUL
    )
    obs = MemoryObservation.objects.create(
        repo_id="group/project",
        activity=activity,
        category=ObservationCategory.BUILD_TEST,
        content="`make test` requires LANGCHAIN_TRACING_V2=false",
    )
    assert obs.status == ObservationStatus.PENDING

    activity.delete()
    obs.refresh_from_db()
    assert obs.activity is None, "FK must be SET_NULL so observations outlive activity retention"


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
