"""``build_session_spend`` — the settled per-model aggregate behind the usage panel.

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

    spend = build_session_spend(session.runs.all())

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
def test_a_name_a_streamed_turn_recorded_twice_over_merges_into_its_one_model(member_user):
    """Rows written before the read-side repair carry the duplicated name (see
    ``collapse_repeated_model_name``). Aggregating verbatim splits one model across two rows
    and prints the doubled name; the price stays lost, because it was never computed.
    """
    session = create_session(member_user)
    _run(
        session,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        usage_by_model={
            "z-ai/glm-5.2z-ai/glm-5.2": {"input_tokens": 60, "output_tokens": 6, "total_tokens": 66},
            "z-ai/glm-5.2": {"input_tokens": 40, "output_tokens": 4, "total_tokens": 44, "cost_usd": "0.10"},
        },
    )

    spend = build_session_spend(session.runs.all())

    assert [row["model"] for row in spend["by_model"]] == ["z-ai/glm-5.2"]
    assert spend["by_model"][0]["total_tokens"] == 110
    assert spend["cost_is_floor"] is True
    # The $0.10 the clean entry carried stays in the session total, but the merged row reads
    # "—": half of it never priced, and one row per model beats two with one name.
    assert spend["cost_usd"] == "0.10"
    assert spend["by_model"][0]["cost_usd"] is None


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

    spend = build_session_spend(session.runs.all())

    assert spend["cost_usd"] == "0.10"
    assert spend["cost_is_floor"] is True
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

    spend = build_session_spend(session.runs.all())

    assert spend["turns"] == 2
    assert spend["total_tokens"] == 110
    assert spend["unrecorded_runs"] == 1
    assert spend["cost_is_floor"] is True


@pytest.mark.django_db
def test_a_session_with_no_runs_is_all_zeroes(member_user):
    spend = build_session_spend(create_session(member_user).runs.all())

    assert spend["turns"] == 0
    assert spend["cost_usd"] == "0"
    assert spend["cost_is_floor"] is False
    assert spend["by_model"] == []


@pytest.mark.django_db
def test_a_corrupt_cost_degrades_to_unpriced_and_is_logged(member_user, caplog):
    """A present-but-unparseable cost is a write-path bug — it must stay visible in the
    logs, not vanish into the same bucket as a legitimately unknown price."""
    session = create_session(member_user)
    _run(
        session,
        input_tokens=10,
        output_tokens=1,
        total_tokens=11,
        usage_by_model={"m1": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11, "cost_usd": "garbage"}},
    )

    with caplog.at_level("WARNING", logger="daiv.sessions"):
        spend = build_session_spend(session.runs.all())

    assert spend["by_model"][0]["cost_usd"] is None
    # The run's only model is unpriced, so there is no total to mark as a floor.
    assert spend["cost_usd"] is None
    assert spend["cost_is_floor"] is False
    assert any("cost_usd" in record.message for record in caplog.records)


@pytest.mark.django_db
def test_a_session_where_nothing_priced_reports_unknown_rather_than_zero(member_user):
    """``"0"`` renders "$0.00+", which reads as "nearly free" rather than "we have no idea" —
    and the footnote that used to disambiguate it is gone. None drops the segment instead.
    """
    session = create_session(member_user)
    _run(
        session,
        input_tokens=100,
        output_tokens=10,
        total_tokens=110,
        usage_by_model={"mystery": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}},
    )

    spend = build_session_spend(session.runs.all())

    assert spend["cost_usd"] is None
    assert spend["total_tokens"] == 110


@pytest.mark.django_db
def test_a_run_recording_tokens_but_no_per_model_usage_is_unknown_too(member_user):
    """It reaches the same "nothing priced" state by a different route, and adds no name to the
    unpriced set — so keying the rule on that set would report "$0.00" here.
    """
    session = create_session(member_user)
    _run(session, input_tokens=100, output_tokens=10, total_tokens=110, usage_by_model={})

    spend = build_session_spend(session.runs.all())

    assert spend["cost_usd"] is None
    assert spend["total_tokens"] == 110
    assert spend["by_model"] == []
