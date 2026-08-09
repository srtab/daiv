import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError

import pytest
from sessions.envelopes import ACTIONABLE_SCHEMA_VERSION, build_actionable_item, validate_actionable
from sessions.models import EnvelopeStatus, OfferedAction, Run, RunEnvelope, RunStatus, Session, SessionOrigin

pytestmark = pytest.mark.django_db


def _mk_session(**kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.SCHEDULE, "repo_id": "group/repo"}
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _mk_run(session: Session, **kwargs) -> Run:
    defaults = {"trigger_type": SessionOrigin.SCHEDULE, "repo_id": session.repo_id, "status": RunStatus.SUCCESSFUL}
    defaults.update(kwargs)
    return Run.objects.create(session=session, **defaults)


def _mk_envelope(run: Run, **overrides) -> RunEnvelope:
    defaults = {"status": EnvelopeStatus.ALL_CLEAR}
    defaults.update(overrides)
    return RunEnvelope.objects.create(run=run, **defaults)


def _item(item_id: str = "f1", **overrides) -> dict:
    item = build_actionable_item(id=item_id, kind="finding", label="Fix the bug", ref="app/module.py:10")
    item.update(overrides)
    return item


# --- AC1: model exists, 1:1 with Run, status indexed + DB-pinned -----------


def test_run_envelope_status_constraint_literals_match_enum() -> None:
    """The ``run_envelope_status_valid`` CHECK references ``EnvelopeStatus.values`` so it cannot
    drift; assert that intent-documenting equivalence (mirrors the ``Run`` drift guard).
    """
    constraint = next(c for c in RunEnvelope._meta.constraints if c.name == "run_envelope_status_valid")
    conditions = dict(constraint.condition.children)
    assert set(conditions["status__in"]) == set(EnvelopeStatus.values)


def test_valid_all_clear_envelope_persists() -> None:
    env = _mk_envelope(_mk_run(_mk_session()), status=EnvelopeStatus.ALL_CLEAR)
    env.refresh_from_db()
    assert env.status == EnvelopeStatus.ALL_CLEAR
    assert env.count == 0
    assert env.summary == ""
    assert env.actionable == []
    assert env.offered_action == OfferedAction.NONE
    assert env.is_actionable is False


def test_bogus_status_create_rejected_by_check_constraint() -> None:
    run = _mk_run(_mk_session())
    with pytest.raises(IntegrityError):
        RunEnvelope.objects.create(run=run, status="bogus")


def test_bogus_status_update_rejected_by_check_constraint() -> None:
    """``.update()`` bypasses field validation, so the DB CHECK is the real guarantee."""
    env = _mk_envelope(_mk_run(_mk_session()), status=EnvelopeStatus.ALL_CLEAR)
    with pytest.raises(IntegrityError):
        RunEnvelope.objects.filter(pk=env.pk).update(status="bogus")


def test_duplicate_envelope_for_same_run_rejected() -> None:
    run = _mk_run(_mk_session())
    _mk_envelope(run, status=EnvelopeStatus.ALL_CLEAR)
    with pytest.raises(IntegrityError):
        _mk_envelope(run, status=EnvelopeStatus.NEEDS_ATTENTION)


# --- AC2: actionable[] item contract ---------------------------------------


def test_validate_actionable_accepts_well_formed_list() -> None:
    validate_actionable([_item("a"), _item("b")])  # no raise


def test_validate_actionable_rejects_non_list() -> None:
    with pytest.raises(ValidationError):
        validate_actionable({"id": "x"})


def test_validate_actionable_rejects_non_dict_item() -> None:
    with pytest.raises(ValidationError):
        validate_actionable(["not-a-mapping"])


def test_validate_actionable_rejects_missing_required_key() -> None:
    item = _item()
    del item["ref"]
    with pytest.raises(ValidationError):
        validate_actionable([item])


def test_validate_actionable_rejects_forbidden_status_key() -> None:
    with pytest.raises(ValidationError):
        validate_actionable([_item(status="found-issues")])


def test_validate_actionable_rejects_duplicate_id() -> None:
    with pytest.raises(ValidationError):
        validate_actionable([_item("dup"), _item("dup")])


def test_build_actionable_item_stamps_schema_version() -> None:
    item = build_actionable_item(id="f1", kind="finding", label="L", ref="r")
    assert item == {
        "id": "f1",
        "kind": "finding",
        "label": "L",
        "ref": "r",
        "schema_version": ACTIONABLE_SCHEMA_VERSION,
    }
    assert item["schema_version"] == 1
    assert "fix_prompt" not in item


def test_build_actionable_item_includes_fix_prompt_when_given() -> None:
    item = build_actionable_item(id="f1", kind="finding", label="L", ref="r", fix_prompt="patch it")
    assert item["fix_prompt"] == "patch it"


def test_full_clean_rejects_malformed_actionable() -> None:
    env = RunEnvelope(run=_mk_run(_mk_session()), status=EnvelopeStatus.NEEDS_ATTENTION, actionable=[{"id": "x"}])
    with pytest.raises(ValidationError):
        env.full_clean()


# --- AC3: shared status -> offered-action mapping --------------------------


def test_offered_action_all_clear() -> None:
    env = RunEnvelope(status=EnvelopeStatus.ALL_CLEAR)
    assert env.offered_action == OfferedAction.NONE
    assert env.is_actionable is False


def test_offered_action_needs_attention() -> None:
    env = RunEnvelope(status=EnvelopeStatus.NEEDS_ATTENTION)
    assert env.offered_action == OfferedAction.REVIEW
    assert env.is_actionable is True


def test_offered_action_failed() -> None:
    env = RunEnvelope(status=EnvelopeStatus.FAILED)
    assert env.offered_action == OfferedAction.RETRY
    assert env.is_actionable is True


def test_offered_action_found_issues_with_actionable_is_fix() -> None:
    env = RunEnvelope(status=EnvelopeStatus.FOUND_ISSUES, actionable=[_item()])
    assert env.offered_action == OfferedAction.FIX
    assert env.is_actionable is True


def test_offered_action_found_issues_empty_resolves_to_none() -> None:
    env = RunEnvelope(status=EnvelopeStatus.FOUND_ISSUES, actionable=[])
    assert env.offered_action == OfferedAction.NONE
    assert env.is_actionable is False


def test_clean_rejects_found_issues_with_empty_actionable() -> None:
    env = RunEnvelope(run=_mk_run(_mk_session()), status=EnvelopeStatus.FOUND_ISSUES, actionable=[])
    with pytest.raises(ValidationError):
        env.full_clean()


def test_clean_accepts_found_issues_with_actionable() -> None:
    env = RunEnvelope(run=_mk_run(_mk_session()), status=EnvelopeStatus.FOUND_ISSUES, actionable=[_item()])
    env.full_clean()  # no raise
    env.save()
    env.refresh_from_db()
    assert env.offered_action == OfferedAction.FIX


# --- AC4: status is read through an accessor, never recomputed --------------


def test_for_run_returns_envelope() -> None:
    run = _mk_run(_mk_session())
    env = _mk_envelope(run)
    assert RunEnvelope.objects.for_run(run) == env


def test_for_run_returns_none_for_pending_run() -> None:
    run = _mk_run(_mk_session())
    assert RunEnvelope.objects.for_run(run) is None


def test_reverse_accessor_run_envelope() -> None:
    run = _mk_run(_mk_session())
    env = _mk_envelope(run)
    run.refresh_from_db()
    assert run.envelope == env


# --- Review-hardening regressions (Story 1.2 review pass) -------------------


def test_validate_actionable_rejects_non_string_id_without_typeerror() -> None:
    """An unhashable/non-string id must surface as ValidationError, never a bare TypeError."""
    with pytest.raises(ValidationError):
        validate_actionable([_item(id=["not", "a", "string"])])


@pytest.mark.parametrize(
    "bad",
    [{"kind": 1}, {"label": None}, {"ref": []}, {"schema_version": "1"}, {"schema_version": True}, {"fix_prompt": 5}],
)
def test_validate_actionable_rejects_wrong_value_types(bad) -> None:
    with pytest.raises(ValidationError):
        validate_actionable([_item(**bad)])


def test_offered_action_unset_status_resolves_to_none() -> None:
    """A freshly built envelope (status unset) must not raise; it offers no action."""
    env = RunEnvelope()
    assert env.offered_action == OfferedAction.NONE
    assert env.is_actionable is False


def test_ordering_has_deterministic_tiebreaker() -> None:
    assert RunEnvelope._meta.ordering == ["-created_at", "-id"]


# --- Epic 1 review pass (2026-07-16): fix_prompt hygiene + envelope coherence ---


def test_build_actionable_item_omits_whitespace_only_fix_prompt() -> None:
    """A whitespace-only ``fix_prompt`` carries no instruction and must be dropped, not stored."""
    item = build_actionable_item(id="f1", kind="finding", label="L", ref="r", fix_prompt="   ")
    assert "fix_prompt" not in item


def test_build_actionable_item_strips_stored_fix_prompt() -> None:
    """A stored ``fix_prompt`` is stripped so leading/trailing noise never reaches the fix agent."""
    item = build_actionable_item(id="f1", kind="finding", label="L", ref="r", fix_prompt="  patch it  ")
    assert item["fix_prompt"] == "patch it"


def test_clean_rejects_non_found_issues_carrying_actionable() -> None:
    """The reverse coherence direction: only a found-issues envelope may carry actionable items."""
    env = RunEnvelope(run=_mk_run(_mk_session()), status=EnvelopeStatus.ALL_CLEAR, actionable=[_item()])
    with pytest.raises(ValidationError):
        env.full_clean()


def test_count_is_derived_from_actionable_on_save() -> None:
    """``count`` mirrors ``len(actionable)`` regardless of any value a caller passes to ``create``."""
    run = _mk_run(_mk_session())
    env = RunEnvelope.objects.create(
        run=run, status=EnvelopeStatus.FOUND_ISSUES, actionable=[_item("a"), _item("b")], count=99
    )
    env.refresh_from_db()
    assert env.count == 2
