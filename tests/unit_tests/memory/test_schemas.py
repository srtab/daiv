import json

import pytest
from memory.schemas import (
    CONTENT_HARD_LIMIT,
    MIN_CONTENT_CHARS,
    ExtractedObservation,
    ExtractedObservations,
    MemoryOperation,
    MemoryOperations,
)
from pydantic import ValidationError

ENTRY_A = "11111111-1111-1111-1111-111111111111"
ENTRY_B = "22222222-2222-2222-2222-222222222222"
OBS = "33333333-3333-3333-3333-333333333333"
OBS_B = "44444444-4444-4444-4444-444444444444"


def _operation(**kwargs):
    kwargs.setdefault("observation_ids", [OBS])
    return MemoryOperation(**kwargs)


class TestIdDeduplication:
    def test_repeated_entry_id_is_collapsed(self):
        # A repeated id would otherwise satisfy MERGE's "two or more", supersede one entry twice
        # and orphan its successor link.
        operation = _operation(op="MERGE", entry_ids=[ENTRY_A, ENTRY_A], category="pitfall", content="x")
        assert operation.entry_ids == [ENTRY_A]

    def test_repeated_observation_id_is_collapsed(self):
        operation = _operation(op="DISCARD", observation_ids=[OBS, OBS], reason="noise")
        assert operation.observation_ids == [OBS]

    def test_first_occurrence_order_is_preserved(self):
        operation = _operation(op="MERGE", entry_ids=[ENTRY_B, ENTRY_A, ENTRY_B], content="x")
        assert operation.entry_ids == [ENTRY_B, ENTRY_A]

    def test_dedup_cannot_be_undone_by_assignment(self):
        # Field validators do not run on assignment, so without frozen=True this would restore the
        # duplicate and let a MERGE-of-one pass the arity check and supersede one entry twice.
        operation = _operation(op="MERGE", entry_ids=[ENTRY_A, ENTRY_B], content="x")
        with pytest.raises(ValidationError):
            operation.entry_ids = [ENTRY_A, ENTRY_A]


class TestShapeError:
    def test_well_formed_operations_report_no_error(self):
        assert _operation(op="ADD", category="pitfall", content="a durable fact").shape_error() is None
        assert _operation(op="UPDATE", entry_ids=[ENTRY_A], content="a corrected fact").shape_error() is None
        assert _operation(op="MERGE", entry_ids=[ENTRY_A, ENTRY_B], content="a combined fact").shape_error() is None
        assert _operation(op="CONFIRM", entry_ids=[ENTRY_A]).shape_error() is None
        assert _operation(op="DISCARD", reason="noise").shape_error() is None

    def test_operation_without_observations_is_rejected(self):
        # Every operation must be attributable to the observations that motivated it.
        operation = MemoryOperation(op="UPDATE", entry_ids=[ENTRY_A], content="unattributed")
        assert operation.shape_error() == "references no observation"

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            (
                {"op": "ADD", "entry_ids": [ENTRY_A], "category": "pitfall", "content": "x"},
                "ADD must not target existing entries",
            ),
            ({"op": "ADD", "category": "pitfall"}, "ADD without content"),
            ({"op": "ADD", "category": "pitfall", "content": "   \n  "}, "ADD without content"),
            ({"op": "ADD", "content": "x"}, "ADD without a category"),
            ({"op": "UPDATE", "content": "x"}, "UPDATE must target exactly one entry, got 0"),
            (
                {"op": "UPDATE", "entry_ids": [ENTRY_A, ENTRY_B], "content": "x"},
                "UPDATE must target exactly one entry, got 2",
            ),
            ({"op": "UPDATE", "entry_ids": [ENTRY_A]}, "UPDATE without content"),
            ({"op": "MERGE", "entry_ids": [ENTRY_A], "content": "x"}, "MERGE must target at least two entries, got 1"),
            (
                {"op": "MERGE", "entry_ids": [ENTRY_A, ENTRY_A], "content": "x"},
                "MERGE must target at least two entries, got 1",
            ),
            ({"op": "MERGE", "entry_ids": [ENTRY_A, ENTRY_B]}, "MERGE without content"),
            ({"op": "CONFIRM"}, "CONFIRM must target exactly one entry, got 0"),
            ({"op": "CONFIRM", "entry_ids": [ENTRY_A, ENTRY_B]}, "CONFIRM must target exactly one entry, got 2"),
            ({"op": "DISCARD", "entry_ids": [ENTRY_A], "reason": "x"}, "DISCARD must not target existing entries"),
            ({"op": "DISCARD"}, "DISCARD without a reason"),
            ({"op": "DISCARD", "reason": "  "}, "DISCARD without a reason"),
        ],
        ids=[
            "add-naming-entry",
            "add-without-content",
            "add-whitespace-content",
            "add-without-category",
            "update-over-zero",
            "update-over-many",
            "update-without-content",
            "merge-of-one",
            "merge-of-one-repeated-twice",
            "merge-without-content",
            "confirm-over-zero",
            "confirm-over-many",
            "discard-naming-entry",
            "discard-without-reason",
            "discard-whitespace-reason",
        ],
    )
    def test_malformed_operation_reports_its_reason(self, kwargs, expected):
        assert _operation(**kwargs).shape_error() == expected

    def test_merge_category_fence_is_not_a_shape_concern(self):
        # Crossing categories needs the round's entry snapshot, so it is the applier's check.
        operation = _operation(op="MERGE", entry_ids=[ENTRY_A, ENTRY_B], category="pitfall", content="a merged fact")
        assert operation.shape_error() is None


@pytest.mark.parametrize("model", [MemoryOperations, ExtractedObservations])
def test_no_length_or_size_constraint_reaches_the_emitted_schema(model):
    # The fence behind every per-item check below: a field constraint is enforced when the
    # structured output is parsed, so one bad value discards every well-formed sibling.
    schema = json.dumps(model.model_json_schema())
    assert "maxLength" not in schema
    assert "minLength" not in schema
    assert "maxItems" not in schema


class TestContentLength:
    """Content length is a per-item rejection, never a payload-wide parse failure."""

    def test_runaway_content_is_rejected_as_a_shape_error(self):
        operation = _operation(op="ADD", category="pitfall", content="x" * (CONTENT_HARD_LIMIT + 1))
        assert operation.shape_error() == f"content is {CONTENT_HARD_LIMIT + 1} characters, over the hard limit"

    def test_content_exactly_at_the_hard_limit_is_accepted(self):
        assert _operation(op="ADD", category="pitfall", content="x" * CONTENT_HARD_LIMIT).shape_error() is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"op": "ADD", "category": "pitfall"},
            {"op": "UPDATE", "entry_ids": [ENTRY_A]},
            {"op": "MERGE", "entry_ids": [ENTRY_A, ENTRY_B]},
        ],
        ids=["add", "update", "merge"],
    )
    def test_content_under_the_floor_is_rejected_for_every_op_that_stores_it(self, kwargs):
        # Entries are append-only, so a two-character entry can only ever be superseded — the
        # durable side must not have a weaker floor than the observations feeding it.
        operation = _operation(**kwargs, content="ok")
        assert operation.shape_error() == f"content is shorter than {MIN_CONTENT_CHARS} characters"

    @pytest.mark.parametrize(
        "kwargs",
        [{"op": "CONFIRM", "entry_ids": [ENTRY_A]}, {"op": "DISCARD", "reason": "ephemeral"}],
        ids=["confirm", "discard"],
    )
    def test_ops_that_never_store_content_are_not_length_checked(self, kwargs):
        # _write never reads content for these two, so rejecting one would re-queue its observation
        # to face the same model over a field that gets thrown away.
        operation = _operation(**kwargs, content="x" * (CONTENT_HARD_LIMIT + 1))
        assert operation.shape_error() is None

    def test_runaway_content_does_not_break_the_payload_parse(self):
        payload = {
            "operations": [
                {"op": "DISCARD", "observation_ids": [OBS], "reason": "ephemeral"},
                {
                    "op": "ADD",
                    "observation_ids": [OBS_B],
                    "category": "codebase_fact",
                    "content": "x" * (CONTENT_HARD_LIMIT + 500),
                },
            ]
        }
        operations = MemoryOperations.model_validate_json(json.dumps(payload)).operations
        assert operations[0].shape_error() is None
        assert operations[1].shape_error() is not None


class TestContentNormalisation:
    """Content is stripped at parse time, so the value checked is the value stored."""

    def test_operation_content_is_stripped_on_parse(self):
        assert _operation(op="ADD", category="pitfall", content=" a durable fact ").content == "a durable fact"

    def test_absent_operation_content_stays_none(self):
        assert _operation(op="CONFIRM", entry_ids=[ENTRY_A]).content is None

    def test_padding_cannot_smuggle_content_past_the_hard_limit(self):
        # The check measured stripped content while the caller persisted it raw, so padding used to
        # land over the fence in the database.
        observation = ExtractedObservation(category="pitfall", content=" " * 50 + "x" * CONTENT_HARD_LIMIT + " " * 50)
        assert observation.shape_error() is None
        assert len(observation.content) == CONTENT_HARD_LIMIT


class TestExtractedObservationShape:
    def test_too_short_observation_is_rejected(self):
        observation = ExtractedObservation(category="workflow", content="n/a")
        assert observation.shape_error() == f"content is shorter than {MIN_CONTENT_CHARS} characters"

    def test_runaway_observation_is_rejected(self):
        observation = ExtractedObservation(category="pitfall", content="x" * (CONTENT_HARD_LIMIT + 1))
        assert observation.shape_error() == f"content is {CONTENT_HARD_LIMIT + 1} characters, over the hard limit"

    def test_unusable_observation_does_not_break_the_payload_parse(self):
        payload = {
            "observations": [
                {"category": "workflow", "content": "n/a"},
                {"category": "build_test", "content": "`make test` sets LANGCHAIN_TRACING_V2=false"},
            ]
        }
        observations = ExtractedObservations.model_validate_json(json.dumps(payload)).observations
        assert [bool(observation.shape_error()) for observation in observations] == [True, False]
