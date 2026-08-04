import pytest
from memory.schemas import MemoryOperation

ENTRY_A = "11111111-1111-1111-1111-111111111111"
ENTRY_B = "22222222-2222-2222-2222-222222222222"
OBS = "33333333-3333-3333-3333-333333333333"


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


class TestShapeError:
    def test_well_formed_operations_report_no_error(self):
        assert _operation(op="ADD", category="pitfall", content="a fact").shape_error() is None
        assert _operation(op="UPDATE", entry_ids=[ENTRY_A], content="a fix").shape_error() is None
        assert _operation(op="MERGE", entry_ids=[ENTRY_A, ENTRY_B], content="merged").shape_error() is None
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
        operation = _operation(op="MERGE", entry_ids=[ENTRY_A, ENTRY_B], category="pitfall", content="x")
        assert operation.shape_error() is None
