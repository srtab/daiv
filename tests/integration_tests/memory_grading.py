"""Deterministic grading for the repository-memory quality suites, plus the judge wrappers.

The `description_shape.py` analogue. Two jobs: fail a malformed case at *collection* rather than
after one paid model call per case per model, and answer every question that can be answered
without a model — emitted counts, observation shape, which operation claimed which observation,
exact duplicate bullets. The judge is called only for the questions that genuinely need one.

Nothing here may import a chat model at module import time: the Provider table is populated by
a session fixture that runs after collection.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Sequence

_TERMINAL_STATUSES = frozenset({"SUCCESSFUL", "FAILED"})
_OPS = frozenset({"ADD", "UPDATE", "MERGE", "CONFIRM", "DISCARD"})
_CATEGORIES = frozenset({"build_test", "codebase_fact", "pitfall", "reviewer_preference", "workflow"})

EXTRACTION_KEYS = frozenset({"id", "messages_path", "status", "memory", "expect"})
_EXTRACTION_EXPECT_KEYS = frozenset({"must_capture", "must_not_capture", "max_observations"})
CONSOLIDATION_KEYS = frozenset({"id", "entries", "observations", "batches", "expect"})
_CONSOLIDATION_EXPECT_KEYS = frozenset({"decisions", "one_operation_for", "content_must_state", "final"})
_FINAL_KEYS = frozenset({"max_entries", "must_state", "no_duplicate_facts"})


def validate_extraction_case(case: dict) -> None:
    """Reject a malformed extraction case."""
    if unknown := set(case) - EXTRACTION_KEYS:
        raise ValueError(f"Unknown extraction case key(s) in {case.get('id')}: {sorted(unknown)}")
    if not case.get("id") or not case.get("messages_path"):
        raise ValueError(f"Extraction case needs an id and a messages_path: {case}")
    if (status := case.get("status")) not in _TERMINAL_STATUSES:
        raise ValueError(f"{case['id']}: status must be one of {sorted(_TERMINAL_STATUSES)}, got {status!r}")
    if "memory" in case and not isinstance(case["memory"], str):
        raise ValueError(f"{case['id']}: memory must be a string")

    expect = case.get("expect", {})
    if unknown := set(expect) - _EXTRACTION_EXPECT_KEYS:
        raise ValueError(f"{case['id']}: unknown expect key(s) {sorted(unknown)}")
    plants = expect.get("must_capture", [])
    decoys = expect.get("must_not_capture", [])
    for name, values in (("must_capture", plants), ("must_not_capture", decoys)):
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f"{case['id']}: {name} must be a list of non-empty strings")
    if overlap := set(plants) & set(decoys):
        raise ValueError(f"{case['id']}: {sorted(overlap)} is both a plant and a decoy")
    if len(set(plants)) != len(plants):
        raise ValueError(f"{case['id']}: must_capture has duplicate entries; the judge matches rows by text")
    cap = expect.get("max_observations")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
        raise ValueError(f"{case['id']}: max_observations must be a non-negative int")


def validate_consolidation_case(case: dict) -> None:
    """Reject a malformed consolidation case, single-round or multi-round."""
    if unknown := set(case) - CONSOLIDATION_KEYS:
        raise ValueError(f"Unknown consolidation case key(s) in {case.get('id')}: {sorted(unknown)}")
    if not case.get("id"):
        raise ValueError(f"Consolidation case needs an id: {case}")
    if ("observations" in case) == ("batches" in case):
        raise ValueError(f"{case['id']}: give exactly one of observations (single-round) or batches (multi-round)")

    entry_ids = {row["id"] for row in _rows(case, "entries")}
    batches = case.get("batches") or [case.get("observations", [])]
    observation_ids = {row["id"] for batch in batches for row in batch}
    for row in [*_rows(case, "entries"), *(r for batch in batches for r in batch)]:
        if row.get("category") not in _CATEGORIES:
            raise ValueError(f"{case['id']}: bad category {row.get('category')!r} on {row.get('id')}")
        if not str(row.get("content", "")).strip():
            raise ValueError(f"{case['id']}: {row.get('id')} has no content")

    expect = case.get("expect", {})
    if unknown := set(expect) - _CONSOLIDATION_EXPECT_KEYS:
        raise ValueError(f"{case['id']}: unknown expect key(s) {sorted(unknown)}")

    for observation_id, decision in expect.get("decisions", {}).items():
        if observation_id not in observation_ids:
            raise ValueError(f"{case['id']}: decision names unknown observation {observation_id}")
        ops = decision["op"] if isinstance(decision["op"], list) else [decision["op"]]
        if bad := [op for op in ops if op not in _OPS]:
            raise ValueError(f"{case['id']}: unknown operation(s) {bad}")
        if unknown := set(decision.get("entries", [])) - entry_ids:
            raise ValueError(f"{case['id']}: decision names unknown entr(ies) {sorted(unknown)}")
    for group in expect.get("one_operation_for", []):
        if len(group) < 2:
            raise ValueError(f"{case['id']}: a one_operation_for group needs at least two observation ids")
        if unknown := set(group) - observation_ids:
            raise ValueError(f"{case['id']}: one_operation_for names unknown observation(s) {sorted(unknown)}")
    for observation_id in expect.get("content_must_state", {}):
        if observation_id not in observation_ids:
            raise ValueError(f"{case['id']}: content_must_state names unknown observation {observation_id}")

    final = expect.get("final", {})
    if unknown := set(final) - _FINAL_KEYS:
        raise ValueError(f"{case['id']}: unknown final key(s) {sorted(unknown)}")
    if "batches" in case and not final:
        raise ValueError(f"{case['id']}: a multi-round case must assert on expect.final")


def _rows(case: dict, key: str) -> list[dict]:
    return case.get(key) or []


def load_messages(payload: Sequence[dict]) -> list[Any]:
    """Turn a case's JSON message rows into the LangChain messages ``serialize_transcript`` reads.

    ``tool_call_id`` and tool-call ``id`` are synthesized: the serializer ignores both, but the
    message constructors require them.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    messages: list[Any] = []
    for index, row in enumerate(payload):
        match row["type"]:
            case "human":
                messages.append(HumanMessage(content=row["content"]))
            case "ai":
                tool_calls = [
                    {"name": call["name"], "args": call.get("args", {}), "id": call.get("id", f"call-{index}-{n}")}
                    for n, call in enumerate(row.get("tool_calls", []))
                ]
                messages.append(AIMessage(content=row.get("content", ""), tool_calls=tool_calls))
            case "tool":
                messages.append(
                    ToolMessage(
                        content=row["content"],
                        name=row.get("name", "tool"),
                        tool_call_id=row.get("tool_call_id", f"call-{index}"),
                    )
                )
            case unhandled:
                raise ValueError(f"unknown message type {unhandled!r}")
    return messages


def extraction_violations(observations: Sequence[Any], expect: dict) -> list[str]:
    """Every deterministic way this emission breaks ``expect``, so one run reports all of them."""
    violations: list[str] = []
    if not expect.get("must_capture"):
        if observations:
            violations.append(f"expected no observations (must_capture is empty), got {len(observations)}")
        return violations

    cap = expect.get("max_observations")
    if cap is not None and len(observations) > cap:
        violations.append(f"emitted {len(observations)}, over the max_observations cap of {cap}")
    violations.extend(
        f"observation {index} fails shape: {reason}"
        for index, observation in enumerate(observations)
        if (reason := observation.shape_error())
    )
    return violations


def decision_violations(applied: dict[str, dict], expect: dict) -> list[str]:
    """Compare the round's operations against the expected per-observation decisions.

    ``applied`` maps an observation id to ``{"op": str, "entries": set[str], "operation_key": str}``,
    the last identifying which operation claimed it. The primary gate, and it never calls a model.
    """
    violations: list[str] = []
    for group in expect.get("one_operation_for", []):
        keys = {applied[oid]["operation_key"] for oid in group if oid in applied}
        if len(keys) > 1:
            violations.append(
                f"observations {sorted(group)} were covered by {len(keys)} separate operations; "
                "one operation should name all of them"
            )
    for observation_id, decision in expect.get("decisions", {}).items():
        actual = applied.get(observation_id)
        if actual is None:
            violations.append(f"observation {observation_id} was left unclaimed by every operation")
            continue
        allowed = decision["op"] if isinstance(decision["op"], list) else [decision["op"]]
        if actual["op"] not in allowed:
            violations.append(f"observation {observation_id}: got {actual['op']}, expected one of {allowed}")
        if (expected_entries := set(decision.get("entries", []))) != actual["entries"]:
            violations.append(
                f"observation {observation_id}: targeted entries {sorted(actual['entries'])}, "
                f"expected {sorted(expected_entries)}"
            )
    return violations


_BULLET = re.compile(r"^\s*-\s+(.*\S)\s*$", re.MULTILINE)


def normalise_bullet(text: str) -> str:
    return " ".join(text.split()).casefold().rstrip(".!?;:")


def duplicate_bullets(document: str) -> list[str]:
    """Bullets that repeat verbatim once normalised — the free half of duplicate detection."""
    seen: set[str] = set()
    repeats: list[str] = []
    for match in _BULLET.finditer(document):
        normalised = normalise_bullet(match.group(1))
        if normalised in seen and normalised not in repeats:
            repeats.append(normalised)
        seen.add(normalised)
    return repeats


def match_claims(expected: Sequence[str], rows: Sequence[Any]) -> tuple[dict[str, bool], list[str]]:
    """Map judge rows back to the claims they grade, by text.

    Index-aligned ``list[bool]`` is not used: models drop and reorder list items, and a silently
    short list would grade the wrong plant. A missing or duplicated row is an error, reported
    post-parse rather than as a pydantic length constraint — a field constraint fails the whole
    structured-output payload, and gateways ignore ``minItems`` anyway.
    """
    verdicts: dict[str, bool] = {}
    errors: list[str] = []
    by_claim: dict[str, list[bool]] = {}
    for row in rows:
        claim = row["claim"] if isinstance(row, dict) else row.claim
        present = row["present"] if isinstance(row, dict) else row.present
        by_claim.setdefault(claim, []).append(present)

    for claim in expected:
        graded = by_claim.get(claim)
        if graded is None:
            errors.append(f"the judge returned no verdict for: {claim!r}")
        elif len(graded) > 1:
            errors.append(f"the judge returned {len(graded)} verdicts for: {claim!r}")
        else:
            verdicts[claim] = graded[0]
    if extra := set(by_claim) - set(expected):
        errors.append(f"the judge invented verdict(s) for: {sorted(extra)}")
    return verdicts, errors


_VOTES: list[tuple[str, str, list[bool]]] = []


def record_votes(suite: str, label: str, results: list[bool]) -> None:
    """Record one case-model's per-repetition outcomes for the end-of-run report."""
    _VOTES.append((suite, label, results))


def votes_report() -> list[str]:
    """The per-case-per-model majority and raw vote split, for the PR body."""
    if not _VOTES:
        return []
    lines = ["", "memory eval — majority outcome and raw vote split (per case per model):"]
    unstable = 0
    for suite, label, results in sorted(_VOTES):
        passes = sum(results)
        total = len(results)
        majority = "PASS" if passes * 2 > total else "FAIL"
        stability = ""
        if 0 < passes < total:
            stability = "  UNSTABLE (excluded from any delta)"
            unstable += 1
        lines.append(f"  {majority}  {passes}/{total}  {suite}::{label}{stability}")
    lines.append(f"  {len(_VOTES)} case-model pair(s), {unstable} unstable.")
    return lines


class ClaimVerdict(BaseModel):
    claim: str = Field(description="The claim being graded, echoed back VERBATIM from the input.")
    present: bool = Field(description="Whether the emitted set states this claim.")


class ClaimVerdicts(BaseModel):
    verdicts: list[ClaimVerdict] = Field(
        default_factory=list, description="Exactly one row per claim given, each echoing its claim verbatim."
    )


class DuplicateVerdict(BaseModel):
    has_duplicate_facts: bool = Field(description="Whether any two bullets state the same fact in different words.")
    explanation: str = Field(description="If true, name the two bullets. If false, one short sentence.")


def _judge():
    from automation.agent.base import BaseAgent, ThinkingLevel

    from .utils import MEMORY_JUDGE_MODEL

    return BaseAgent.get_model(model=MEMORY_JUDGE_MODEL, thinking_level=ThinkingLevel.MEDIUM)


async def judge_claims(
    emitted: Sequence[str], claims: Sequence[str], *, what: str
) -> tuple[dict[str, bool], list[str]]:
    """One judge call grading whether each claim is stated by ``emitted``."""
    if not claims:
        return {}, []
    emitted_text = "\n".join(f"- {text}" for text in emitted) or "(nothing was emitted)"
    claims_text = "\n".join(f"- {claim}" for claim in claims)
    prompt = (
        f"You are grading a repository-memory pipeline. Below is {what}, then a list of claims.\n\n"
        f"Emitted:\n{emitted_text}\n\nClaims:\n{claims_text}\n\n"
        "For EACH claim, return one row: the claim echoed back verbatim, and whether the emitted "
        "text states that claim (in any wording). Judge meaning, not phrasing. Return exactly one "
        "row per claim, no more and no fewer."
    )
    result = await _judge().with_structured_output(ClaimVerdicts).ainvoke(prompt)
    return match_claims(list(claims), result.verdicts if result else [])


async def judge_duplicate_facts(document: str) -> tuple[bool, str]:
    """One judge call asking whether any two bullets state the same fact in different words."""
    prompt = (
        "Below is a repository's rendered memory document. Do any two bullets state the SAME fact "
        "in different words? Near-duplicates and fragments of one fact count; two different facts "
        "about the same command do not.\n\n"
        f"{document}"
    )
    result = await _judge().with_structured_output(DuplicateVerdict).ainvoke(prompt)
    if result is None:
        return True, "the judge returned nothing"
    return result.has_duplicate_facts, result.explanation
