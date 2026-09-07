import pytest

from tests.integration_tests.memory_grading import (
    _CATEGORIES,
    _OPS,
    _TERMINAL_STATUSES,
    _VOTES,
    ClaimVerdict,
    decision_violations,
    duplicate_bullets,
    extraction_violations,
    load_messages,
    match_claims,
    record_votes,
    validate_consolidation_case,
    validate_extraction_case,
    votes_report,
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

    def test_rejects_a_case_with_no_expect_block(self):
        with pytest.raises(ValueError, match="expect"):
            validate_extraction_case({"id": "x", "messages_path": "m.json", "status": "SUCCESSFUL"})

    def test_rejects_expect_given_as_a_list(self):
        case = {"id": "x", "messages_path": "m.json", "status": "SUCCESSFUL", "expect": ["must_capture"]}
        with pytest.raises(ValueError, match="expect must be a dict"):
            validate_extraction_case(case)

    def test_rejects_must_capture_with_a_zero_cap(self):
        case = {
            "id": "x",
            "messages_path": "m.json",
            "status": "SUCCESSFUL",
            "expect": {"must_capture": ["a fact"], "max_observations": 0},
        }
        with pytest.raises(ValueError, match="can never pass"):
            validate_extraction_case(case)

    def test_rejects_duplicate_must_not_capture_entries(self):
        case = {
            "id": "x",
            "messages_path": "m.json",
            "status": "SUCCESSFUL",
            "expect": {"must_capture": [], "must_not_capture": ["a fact", "a fact"]},
        }
        with pytest.raises(ValueError, match="duplicate"):
            validate_extraction_case(case)

    def test_rejects_near_duplicate_must_capture_entries(self):
        case = {
            "id": "x",
            "messages_path": "m.json",
            "status": "SUCCESSFUL",
            "expect": {"must_capture": ["A fact.", "a fact"]},
        }
        with pytest.raises(ValueError, match="duplicate"):
            validate_extraction_case(case)


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

    def test_rejects_neither_observations_nor_batches(self):
        case = {"id": "y", "entries": [], "expect": {}}
        with pytest.raises(ValueError, match="exactly one of"):
            validate_consolidation_case(case)

    def test_rejects_both_observations_and_batches(self):
        row = {"id": "o1", "category": "build_test", "content": "c"}
        case = {"id": "y", "entries": [], "observations": [row], "batches": [[row]], "expect": {"final": {}}}
        with pytest.raises(ValueError, match="exactly one of"):
            validate_consolidation_case(case)

    def test_rejects_an_empty_observations_list(self):
        case = {"id": "y", "entries": [], "observations": [], "expect": {}}
        with pytest.raises(ValueError, match="observations must be non-empty"):
            validate_consolidation_case(case)

    def test_rejects_an_empty_batch(self):
        row = {"id": "o1", "category": "build_test", "content": "c"}
        case = {"id": "y", "entries": [], "batches": [[row], []], "expect": {"final": {"max_entries": 1}}}
        with pytest.raises(ValueError, match="batches must be non-empty"):
            validate_consolidation_case(case)

    def test_rejects_a_row_missing_an_id(self):
        case = {"id": "y", "entries": [], "observations": [{"category": "build_test", "content": "c"}], "expect": {}}
        with pytest.raises(ValueError, match="needs a non-empty string id"):
            validate_consolidation_case(case)

    def test_rejects_duplicate_entry_ids(self):
        case = {
            "id": "y",
            "entries": [
                {"id": "e1", "category": "build_test", "content": "c"},
                {"id": "e1", "category": "build_test", "content": "c2"},
            ],
            "observations": [{"id": "o1", "category": "build_test", "content": "c"}],
            "expect": {},
        }
        with pytest.raises(ValueError, match="entries has duplicate"):
            validate_consolidation_case(case)

    def test_rejects_duplicate_observation_ids_across_batches(self):
        row_a = {"id": "o1", "category": "build_test", "content": "c"}
        row_b = {"id": "o1", "category": "build_test", "content": "c2"}
        case = {"id": "y", "entries": [], "batches": [[row_a], [row_b]], "expect": {"final": {"max_entries": 1}}}
        with pytest.raises(ValueError, match="observations has duplicate"):
            validate_consolidation_case(case)

    def test_rejects_a_decision_missing_an_op(self):
        case = {
            "id": "y",
            "entries": [],
            "observations": [{"id": "o1", "category": "build_test", "content": "c"}],
            "expect": {"decisions": {"o1": {"entries": []}}},
        }
        with pytest.raises(ValueError, match="needs an op"):
            validate_consolidation_case(case)

    def test_rejects_a_multi_round_case_without_a_final_assertion(self):
        row = {"id": "o1", "category": "build_test", "content": "c"}
        case = {"id": "y", "entries": [], "batches": [[row]], "expect": {}}
        with pytest.raises(ValueError, match="expect.final"):
            validate_consolidation_case(case)

    def test_rejects_near_duplicate_final_must_state_entries(self):
        row = {"id": "o1", "category": "build_test", "content": "c"}
        case = {
            "id": "y",
            "entries": [],
            "batches": [[row]],
            "expect": {"final": {"must_state": ["a fact", "A fact."]}},
        }
        with pytest.raises(ValueError, match="duplicate"):
            validate_consolidation_case(case)

    def test_rejects_final_must_state_given_as_a_bare_string(self):
        row = {"id": "o1", "category": "build_test", "content": "c"}
        case = {"id": "y", "entries": [], "batches": [[row]], "expect": {"final": {"must_state": "a bare string"}}}
        with pytest.raises(ValueError, match="must be a list of non-empty strings"):
            validate_consolidation_case(case)

    def test_rejects_content_must_state_value_given_as_a_non_string(self):
        case = {
            "id": "y",
            "entries": [],
            "observations": [{"id": "o1", "category": "build_test", "content": "c"}],
            "expect": {"content_must_state": {"o1": ["not", "a", "string"]}},
        }
        with pytest.raises(ValueError, match="must be a non-empty string"):
            validate_consolidation_case(case)

    def test_rejects_content_must_state_given_as_a_non_dict(self):
        case = {
            "id": "y",
            "entries": [],
            "observations": [{"id": "o1", "category": "build_test", "content": "c"}],
            "expect": {"content_must_state": ["o1"]},
        }
        with pytest.raises(ValueError, match="content_must_state must be a dict"):
            validate_consolidation_case(case)

    def test_rejects_entries_given_as_a_non_list(self):
        case = {
            "id": "y",
            "entries": {"oops": 1},
            "observations": [{"id": "o1", "category": "build_test", "content": "c"}],
            "expect": {},
        }
        with pytest.raises(ValueError, match="entries must be a list of dicts"):
            validate_consolidation_case(case)

    def test_rejects_observations_containing_non_dict_rows(self):
        case = {"id": "y", "entries": [], "observations": ["o1"], "expect": {}}
        with pytest.raises(ValueError, match="observations must be a list of dicts"):
            validate_consolidation_case(case)


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

    def test_an_empty_emission_against_a_non_empty_must_capture_is_a_violation(self):
        violations = extraction_violations([], {"must_capture": ["a plant"], "max_observations": 2})
        assert violations and "emitted nothing" in violations[0]


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

    def test_all_unclaimed_in_a_group_is_a_violation(self):
        violations = decision_violations({}, {"one_operation_for": [["o1", "o2"]]})
        assert violations and "unclaimed" in violations[0]

    def test_decision_missing_op_raises(self):
        applied = {"o1": _applied("ADD")}
        with pytest.raises(ValueError, match="needs an op"):
            decision_violations(applied, {"decisions": {"o1": {"entries": ["e1"]}}})


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

    def test_an_invented_verdict_is_an_error(self):
        rows = [{"claim": "a", "present": True}, {"claim": "z", "present": True}]
        verdicts, errors = match_claims(["a"], rows)
        assert verdicts == {"a": True}
        assert errors and "z" in errors[0]

    def test_matches_despite_whitespace_case_and_punctuation_drift(self):
        rows = [{"claim": " A fact. ", "present": True}]
        verdicts, errors = match_claims(["a fact"], rows)
        assert verdicts == {"a fact": True}
        assert errors == []

    def test_accepts_pydantic_rows_not_only_dicts(self):
        rows = [ClaimVerdict(claim="a", present=True)]
        verdicts, errors = match_claims(["a"], rows)
        assert verdicts == {"a": True}
        assert errors == []


class TestVotesReport:
    def setup_method(self):
        _VOTES.clear()

    def teardown_method(self):
        _VOTES.clear()

    def test_no_votes_produces_an_empty_report(self):
        assert votes_report() == []

    def test_majority_rule_and_the_even_count_tie_direction(self):
        record_votes("suite", "two-of-three", [True, True, False])
        record_votes("suite", "one-of-three", [True, False, False])
        record_votes("suite", "tie-of-two", [True, False])
        record_votes("suite", "tie-of-four", [True, True, False, False])
        record_votes("suite", "one-of-one", [True])
        report = "\n".join(votes_report())
        assert "PASS  2/3  suite::two-of-three" in report
        assert "FAIL  1/3  suite::one-of-three" in report
        assert "FAIL  1/2  suite::tie-of-two" in report
        assert "FAIL  2/4  suite::tie-of-four" in report
        assert "PASS  1/1  suite::one-of-one" in report

    def test_unstable_marker_matches_the_split_vote_condition(self):
        record_votes("suite", "unanimous", [True, True, True])
        record_votes("suite", "split-vote", [True, False, True])
        lines = votes_report()
        unanimous_line = next(line for line in lines if "suite::unanimous" in line)
        split_line = next(line for line in lines if "suite::split-vote" in line)
        assert "UNSTABLE" not in unanimous_line
        assert "UNSTABLE" in split_line


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

    def test_rejects_an_unknown_message_type(self):
        with pytest.raises(ValueError, match="unknown message type"):
            load_messages([{"type": "system", "content": "x"}])


class TestSharedSpan:
    def test_finds_an_eight_word_overlap_regardless_of_case_and_spacing(self):
        from tests.integration_tests.memory_grading import shared_span

        a = "The  end-to-end suite needs a running message broker or every test fails."
        b = "we learned the End-to-end suite needs a running message broker or every test fails at setup"
        assert shared_span(a, b) == "end-to-end suite needs a running message broker or"

    def test_seven_shared_words_is_not_an_overlap(self):
        from tests.integration_tests.memory_grading import shared_span

        assert shared_span("one two three four five six seven", "one two three four five six seven") is None

    def test_unrelated_text_has_no_overlap(self):
        from tests.integration_tests.memory_grading import shared_span

        assert shared_span("the build needs a pinned node version", "reviewers reject raw sql in views") is None


class TestStaticMirrorsStayInSync:
    def test_terminal_statuses_match_run_status(self):
        from sessions.models import RunStatus

        assert set(RunStatus.terminal()) == _TERMINAL_STATUSES

    def test_categories_match_observation_category(self):
        from memory.models import ObservationCategory

        assert set(ObservationCategory.values) == _CATEGORIES

    def test_ops_match_memory_operation_literal(self):
        from typing import get_args

        from memory.schemas import MemoryOperationLiteral

        assert set(get_args(MemoryOperationLiteral)) == _OPS
