"""Scored quality eval for repository-memory extraction.

Cases hold a JSON *message list*, not transcript text, and the harness runs it through
``serialize_transcript`` — otherwise the 1,000-char tool-output cap (cases 001, 005) and the
200-char arg cap (case 004) would go entirely unexercised, and in production the hard-won fact
usually *is* a truncated tool error.

Grading is cheapest-first: the deterministic checks never call a model, and an empty-set case
(the expected outcome for most real runs) is graded for free.
"""

import json
from pathlib import Path

import pytest
from langsmith import testing as t
from memory.extraction import extract_from_transcript
from memory.transcript import serialize_transcript

from .memory_grading import (
    assert_no_few_shot_leak,
    extraction_violations,
    judge_claims,
    load_messages,
    record_votes,
    validate_extraction_case,
)
from .utils import EVAL_REPEATS, MEMORY_EXTRACTION_MODELS, require_provider_for_model

DATA_DIR = Path(__file__).parent / "data" / "memory" / "extraction"
TEST_SUITE = "DAIV: Memory Extraction"


def load_cases():
    for line in (DATA_DIR / "cases.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        validate_extraction_case(case)
        messages = json.loads((DATA_DIR / case.pop("messages_path")).read_text())
        expect = case.get("expect", {})
        # A suppression case (must_capture/must_not_capture both empty, e.g. 006, 008, 010) has no
        # plant or decoy text at all — the graded fact lives only in the transcript and, for a
        # memory-aware case, in ``memory`` — so both must be covered too, not just expect.
        assert_no_few_shot_leak([
            *expect.get("must_capture", []),
            *expect.get("must_not_capture", []),
            case.get("memory", ""),
            *(str(row.get("content", "")) for row in messages),
        ])
        case["messages"] = messages
        yield pytest.param(case, id=case["id"])


async def _attempt(case: dict, model_name: str, transcript: str) -> tuple[bool, str, list[str]]:
    """One graded extraction. Returns (passed, why-not, what-was-emitted)."""
    observations = await extract_from_transcript(
        transcript,
        repo_id=f"eval/{case['id']}",
        status=case["status"],
        memory=case.get("memory", ""),
        model_names=[model_name],
        run_ref=case["id"],
    )
    emitted = [observation.content for observation in observations]
    expect = case.get("expect", {})

    if violations := extraction_violations(observations, expect):
        return False, "; ".join(violations), emitted

    plants = expect.get("must_capture", [])
    decoys = expect.get("must_not_capture", [])
    if not emitted:
        # Nothing was emitted and the deterministic checks passed, so no plant is missed
        # that the checks would not already have caught
        return True, "", emitted

    verdicts, errors = await judge_claims(
        emitted, [*plants, *decoys], what="the observations an extraction run emitted"
    )
    if errors:
        return False, "; ".join(errors), emitted

    problems = [f"missed plant: {plant!r}" for plant in plants if not verdicts[plant]]
    problems += [f"leaked decoy: {decoy!r}" for decoy in decoys if verdicts[decoy]]
    return not problems, "; ".join(problems), emitted


@pytest.mark.memory
@pytest.mark.langsmith(test_suite_name=TEST_SUITE)
@pytest.mark.parametrize("model_name", MEMORY_EXTRACTION_MODELS)
@pytest.mark.parametrize("case", load_cases())
async def test_memory_extraction(case, model_name):
    require_provider_for_model(model_name)

    transcript = serialize_transcript(load_messages(case["messages"]))
    t.log_inputs({
        "case": case["id"],
        "status": case["status"],
        "transcript": transcript,
        "memory": case.get("memory", ""),
    })

    results: list[bool] = []
    details: list[str] = []
    emissions: list[list[str]] = []
    try:
        for repetition in range(EVAL_REPEATS):
            try:
                passed, detail, emitted = await _attempt(case, model_name, transcript)
            except Exception as exc:  # noqa: BLE001 — a crashed attempt must still vote FAIL, not vanish
                passed, detail, emitted = False, f"attempt {repetition} raised {exc!r}", []
            results.append(passed)
            details.append(detail)
            emissions.append(emitted)
    finally:
        # In a finally so a cell that never finishes still leaves a FAIL row in votes_report
        # instead of silently vanishing from Task 10's only source for BASELINE.md.
        record_votes(TEST_SUITE, f"{case['id']}[{model_name}]", results)
        t.log_outputs({"votes": results, "emissions": emissions})

    report = "\n".join(
        f"  attempt {index}: {detail or 'ok'} | emitted={emitted}"
        for index, (detail, emitted) in enumerate(zip(details, emissions, strict=True), start=1)
    )
    assert sum(results) * 2 > EVAL_REPEATS, (
        f"{case['id']} @ {model_name}: {sum(results)}/{EVAL_REPEATS} attempts passed\n{report}"
    )
