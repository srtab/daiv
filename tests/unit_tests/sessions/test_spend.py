"""``build_session_spend`` — the settled per-model aggregate behind the usage panel (§4).

Every ``Run`` row counts toward the turn count regardless of status; rows with no recorded
usage and models with no price each independently turn the totals into floors.
"""

import pytest
from sessions.models import Run, RunStatus, SessionOrigin
from sessions.spend import build_session_spend

from tests.unit_tests.chat.chat_pages import create_session


def _run(session, **kwargs) -> Run:
    defaults = {
        "trigger_type": SessionOrigin.CHAT,
        "status": RunStatus.SUCCESSFUL,
        "repo_id": "group/project",
        "ref": "main",
    }
    defaults.update(kwargs)
    return Run.objects.create(session=session, **defaults)


@pytest.mark.django_db
def test_per_model_merge_across_runs(member_user):
    session = create_session(member_user)
    _run(
        session,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        usage_by_model={"m1": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cost_usd": "0.10"}},
    )
    _run(
        session,
        input_tokens=200,
        output_tokens=20,
        total_tokens=220,
        usage_by_model={
            "m1": {"input_tokens": 150, "output_tokens": 15, "total_tokens": 165, "cost_usd": "0.20"},
            "m2": {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55, "cost_usd": "0.01"},
        },
    )

    spend = build_session_spend(session)

    assert spend["turns"] == 2
    assert spend["total_tokens"] == 330
    assert spend["cost_usd"] == "0.31"
    assert spend["cost_is_floor"] is False
    assert spend["unrecorded_runs"] == 0
    m1 = next(row for row in spend["by_model"] if row["model"] == "m1")
    assert m1["total_tokens"] == 275
    assert m1["cost_usd"] == "0.30"
    assert [row["model"] for row in spend["by_model"]] == ["m1", "m2"]


@pytest.mark.django_db
def test_an_unpriced_model_yields_a_floor_total_not_a_silently_smaller_one(member_user):
    session = create_session(member_user)
    _run(
        session,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        usage_by_model={
            "priced": {"input_tokens": 60, "output_tokens": 6, "total_tokens": 66, "cost_usd": "0.10"},
            "mystery": {"input_tokens": 40, "output_tokens": 4, "total_tokens": 44},
        },
    )

    spend = build_session_spend(session)

    assert spend["cost_usd"] == "0.10"
    assert spend["cost_is_floor"] is True
    assert spend["unpriced_models"] == ["mystery"]
    mystery = next(row for row in spend["by_model"] if row["model"] == "mystery")
    assert mystery["cost_usd"] is None
    assert mystery["total_tokens"] == 44


@pytest.mark.django_db
def test_a_null_usage_run_counts_as_a_turn_adds_nothing_and_forces_the_floor(member_user):
    session = create_session(member_user)
    _run(
        session,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        usage_by_model={"m1": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cost_usd": "0.10"}},
    )
    _run(session, status=RunStatus.FAILED)  # finalize failed: nothing recorded

    spend = build_session_spend(session)

    assert spend["turns"] == 2
    assert spend["total_tokens"] == 110
    assert spend["unrecorded_runs"] == 1
    assert spend["cost_is_floor"] is True


@pytest.mark.django_db
def test_a_session_with_no_runs_is_all_zeroes(member_user):
    spend = build_session_spend(create_session(member_user))

    assert spend["turns"] == 0
    assert spend["cost_usd"] == "0"
    assert spend["cost_is_floor"] is False
    assert spend["by_model"] == []
