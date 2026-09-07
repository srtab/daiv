import pytest

from tests.integration_tests.memory_grading import (
    decision_violations,
    duplicate_bullets,
    extraction_violations,
    load_messages,
    match_claims,
    validate_consolidation_case,
    validate_extraction_case,
)


class _Obs:
    def __init__(self, content, category="build_test"):
        self.content = content
        self.category = category

    def shape_error(self):
        return None if 10 <= len(self.content) <= 2000 else "content is out of bounds"


class TestValidateExtractionCase:
    def test_rejects_an_unknown_key(self):
        with pytest.raises(ValueError, match="Unknown extraction case key"):
            validate_extraction_case({"id": "x", "messages_path": "m.json", "status": "SUCCESSFUL", "nope": 1})

    def test_rejects_a_status_outside_the_terminal_run_statuses(self):
        with pytest.raises(ValueError, match="status"):
            validate_extraction_case({"id": "x", "messages_path": "m.json", "status": "completed"})

    def test_rejects_a_plant_that_is_also_a_decoy(self):
        case = {
            "id": "x",
            "messages_path": "m.json",
            "status": "SUCCESSFUL",
            "expect": {"must_capture": ["a fact"], "must_not_capture": ["a fact"]},
        }
        with pytest.raises(ValueError, match="both a plant and a decoy"):
            validate_extraction_case(case)

    def test_accepts_a_well_formed_case(self):
        validate_extraction_case({
            "id": "x",
            "messages_path": "m.json",
            "status": "FAILED",
            "memory": "## Build & test\n- something",
            "expect": {"must_capture": ["a fact"], "must_not_capture": [], "max_observations": 2},
        })


class TestValidateConsolidationCase:
    def test_rejects_an_expected_op_outside_the_operation_literals(self):
        case = {
            "id": "y",
            "entries": [],
            "observations": [{"id": "o1", "category": "build_test", "content": "c"}],
            "expect": {"decisions": {"o1": {"op": "REMOVE"}}},
        }
        with pytest.raises(ValueError, match="REMOVE"):
            validate_consolidation_case(case)

    def test_rejects_a_decision_naming_an_unknown_observation(self):
        case = {
            "id": "y",
            "entries": [],
            "observations": [{"id": "o1", "category": "build_test", "content": "c"}],
            "expect": {"decisions": {"o9": {"op": "ADD"}}},
        }
        with pytest.raises(ValueError, match="o9"):
            validate_consolidation_case(case)

    def test_rejects_a_decision_naming_an_unknown_entry(self):
        case = {
            "id": "y",
            "entries": [{"id": "e1", "category": "build_test", "content": "c"}],
            "observations": [{"id": "o1", "category": "build_test", "content": "c"}],
            "expect": {"decisions": {"o1": {"op": "UPDATE", "entries": ["e7"]}}},
        }
        with pytest.raises(ValueError, match="e7"):
            validate_consolidation_case(case)

    def test_accepts_an_any_of_op_list(self):
        validate_consolidation_case({
            "id": "y",
            "entries": [{"id": "e1", "category": "build_test", "content": "c"}],
            "observations": [{"id": "o1", "category": "build_test", "content": "c"}],
            "expect": {"decisions": {"o1": {"op": ["UPDATE", "MERGE"], "entries": ["e1"]}}},
        })


class TestExtractionViolations:
    def test_empty_must_capture_demands_an_empty_emission(self):
        violations = extraction_violations([_Obs("something learned here")], {"must_capture": []})
        assert violations and "expected no observations" in violations[0]

    def test_over_the_count_cap_is_a_violation(self):
        observations = [_Obs("a fact worth keeping"), _Obs("another fact worth keeping")]
        violations = extraction_violations(observations, {"must_capture": ["x"], "max_observations": 1})
        assert violations and "over the max_observations" in violations[0]

    def test_reports_every_shape_error_in_one_pass(self):
        violations = extraction_violations([_Obs("short"), _Obs("tiny")], {"must_capture": ["x"]})
        assert len(violations) == 2

    def test_clean_emission_has_no_violations(self):
        assert (
            extraction_violations([_Obs("a fact worth keeping")], {"must_capture": ["x"], "max_observations": 2}) == []
        )


def _applied(op, entries=(), key="op-1"):
    return {"op": op, "entries": set(entries), "operation_key": key}


class TestDecisionViolations:
    def test_matching_op_and_entry_set_passes(self):
        applied = {"o1": _applied("MERGE", ["e1", "e2"])}
        assert decision_violations(applied, {"decisions": {"o1": {"op": "MERGE", "entries": ["e2", "e1"]}}}) == []

    def test_any_of_passes_on_either_member(self):
        applied = {"o1": _applied("UPDATE", ["e1"])}
        expect = {"decisions": {"o1": {"op": ["UPDATE", "MERGE"], "entries": ["e1"]}}}
        assert decision_violations(applied, expect) == []

    def test_wrong_op_is_reported_with_both_values(self):
        violations = decision_violations(
            {"o1": _applied("ADD")}, {"decisions": {"o1": {"op": "CONFIRM", "entries": ["e1"]}}}
        )
        assert violations and "ADD" in violations[0] and "CONFIRM" in violations[0]

    def test_unclaimed_observation_is_reported_by_id(self):
        violations = decision_violations({}, {"decisions": {"o3": {"op": "ADD"}}})
        assert violations and "o3" in violations[0] and "unclaimed" in violations[0]

    def test_wrong_entry_set_is_reported(self):
        applied = {"o1": _applied("MERGE", ["e1", "e3"])}
        violations = decision_violations(applied, {"decisions": {"o1": {"op": "MERGE", "entries": ["e1", "e2"]}}})
        assert violations and "e2" in violations[0]

    def test_two_operations_for_one_group_is_a_violation(self):
        applied = {"o1": _applied("ADD", key="entry-a"), "o2": _applied("ADD", key="entry-b")}
        violations = decision_violations(applied, {"one_operation_for": [["o1", "o2"]]})
        assert violations and "separate operations" in violations[0]

    def test_one_shared_operation_passes(self):
        applied = {"o1": _applied("ADD", key="entry-a"), "o2": _applied("ADD", key="entry-a")}
        assert decision_violations(applied, {"one_operation_for": [["o1", "o2"]]}) == []


class TestDuplicateBullets:
    def test_finds_an_exact_repeat_across_sections(self):
        document = "## Build & test\n- Make Test runs pytest.\n\n## Pitfalls\n- make test runs pytest\n"
        assert duplicate_bullets(document) == ["make test runs pytest"]

    def test_distinct_bullets_are_clean(self):
        assert duplicate_bullets("## Build & test\n- a fact\n- another fact\n") == []


class TestMatchClaims:
    def test_maps_rows_back_by_text(self):
        rows = [{"claim": "b", "present": False}, {"claim": "a", "present": True}]
        verdicts, errors = match_claims(["a", "b"], rows)
        assert verdicts == {"a": True, "b": False}
        assert errors == []

    def test_a_missing_row_is_an_error_not_a_silent_false(self):
        verdicts, errors = match_claims(["a", "b"], [{"claim": "a", "present": True}])
        assert errors and "b" in errors[0]

    def test_a_duplicated_row_is_an_error(self):
        rows = [{"claim": "a", "present": True}, {"claim": "a", "present": False}]
        _verdicts, errors = match_claims(["a"], rows)
        assert errors and "a" in errors[0]


class TestLoadMessages:
    def test_builds_the_three_message_types_serialize_transcript_reads(self):
        from memory.transcript import serialize_transcript

        messages = load_messages([
            {"type": "human", "content": "fix the flake"},
            {"type": "ai", "content": "checking", "tool_calls": [{"name": "bash", "args": {"command": "pytest"}}]},
            {"type": "tool", "name": "bash", "content": "1 failed"},
        ])
        transcript = serialize_transcript(messages)

        assert "[human] fix the flake" in transcript
        assert "[ai:tool_call] bash(" in transcript
        assert "[tool:bash] 1 failed" in transcript
