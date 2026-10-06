"""Deterministic grading for the code-review recall suite, plus the judge call.

The ``memory_grading.py`` analogue: a malformed case fails at collection, before any model call, and everything that
can be answered without a model is answered here — the report's findings, which of them are located in the planted
file, the clean-twin rule and the noise count. The judge sees only findings located in the planted file, one at a time,
and line numbers never decide anything because the model miscounts them.

Nothing here may import a chat model at module import time: the Provider table is populated by a session fixture that
runs after collection.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from .evaluators import Verdict

CASE_KEYS = frozenset({"id", "base_sha", "patch_path", "kind", "twin", "planted"})
PLANTED_KEYS = frozenset({"file", "lines", "defect", "dimension"})
DIMENSIONS = frozenset({"correctness", "security", "performance"})
SEVERITIES = ("Critical", "Important", "Suggestion", "Question")
BLOCKING = frozenset({"Critical", "Important"})

_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEADING = re.compile(r"^#{1,4}\s+(.+?)\s*$")
_REVIEW_HEADING = re.compile(r"^#{1,3}\s*Code Review\b", re.MULTILINE)
_NO_FINDINGS = re.compile(r"^\W*No findings\b", re.MULTILINE)
_DEGRADED = re.compile(r"^.*Review unavailable for.*$", re.MULTILINE)
_SEVERITY_WORD = re.compile(rf"\b({'|'.join(SEVERITIES)})s?\b", re.IGNORECASE)
_ENTRY = re.compile(r"^\s*(?:\*\*)?\s*\d+[.)]\s*(.+?)\s*$")
_TITLE_SPLIT = re.compile(r"\*\*\s*(?:—|–|-|:)?\s*")
_DASH_SPLIT = re.compile(r"\s+(?:—|–|-)\s+")
_LOCATION_LINE = re.compile(r"^\s*[-*]?\s*\**location:?\**:?\s*(.+)$", re.IGNORECASE)
_BLOB_URL = re.compile(r"https?://\S*?/blob/[^/\s)]+/([^\s)#?]+)")
_URL = re.compile(r"https?://\S+")
_FILE_PATH = re.compile(r"(?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z]\w*")
_DETAILS_TAG = re.compile(r"</?(?:details|summary)>")
_SUMMARY_LABEL = re.compile(r"^\s*Details\s*$", re.MULTILINE)


def validate_cases(cases: Sequence[dict], data_dir: Path) -> None:
    """Reject a malformed case set: each case's shape, unique ids, and twins that pair with a bug case."""
    ids = [case.get("id") for case in cases]
    if duplicates := sorted({case_id for case_id in ids if ids.count(case_id) > 1}):
        raise ValueError(f"duplicate case id(s): {duplicates}")
    bugs = {case["id"]: case for case in cases if case.get("kind") == "bug"}
    for case in cases:
        validate_case(case, data_dir, bugs=bugs)


def validate_case(case: dict, data_dir: Path, *, bugs: dict[str, dict]) -> None:
    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"a case needs a non-empty string id: {case}")
    if unknown := set(case) - CASE_KEYS:
        raise ValueError(f"{case_id}: unknown key(s) {sorted(unknown)}")
    if not isinstance(case.get("base_sha"), str) or not _SHA.match(case["base_sha"]):
        raise ValueError(f"{case_id}: base_sha must be a full 40-character lowercase commit sha")
    patch_path = case.get("patch_path")
    if not isinstance(patch_path, str) or not patch_path.endswith(".patch"):
        raise ValueError(f"{case_id}: patch_path must name a .patch file")
    patch = (data_dir / patch_path).resolve()
    if data_dir.resolve() not in patch.parents or not patch.is_file():
        raise ValueError(f"{case_id}: no patch file at {patch_path} under {data_dir}")

    match case.get("kind"):
        case "bug":
            if "twin" in case:
                raise ValueError(f"{case_id}: only a clean case names a twin")
            _validate_planted(case_id, case.get("planted"), patch.read_text(encoding="utf-8"))
        case "clean":
            if "planted" in case:
                raise ValueError(f"{case_id}: a clean case plants nothing")
            twin = bugs.get(case.get("twin"))
            if twin is None:
                raise ValueError(f"{case_id}: twin must name a bug case, got {case.get('twin')!r}")
            if twin["base_sha"] != case["base_sha"]:
                raise ValueError(f"{case_id}: a clean twin must share its bug case's base_sha")
        case other:
            raise ValueError(f"{case_id}: kind must be 'bug' or 'clean', got {other!r}")


def _validate_planted(case_id: str, planted: object, patch: str) -> None:
    if not isinstance(planted, dict) or set(planted) != PLANTED_KEYS:
        raise ValueError(f"{case_id}: planted needs exactly {sorted(PLANTED_KEYS)}")
    if not isinstance(planted["file"], str) or f"+++ b/{planted['file']}\n" not in patch:
        raise ValueError(f"{case_id}: the patch does not change planted.file {planted['file']!r}")
    lines = planted["lines"]
    if (
        not isinstance(lines, list)
        or len(lines) != 2
        or not all(isinstance(line, int) and not isinstance(line, bool) for line in lines)
        or not 1 <= lines[0] <= lines[1]
    ):
        raise ValueError(f"{case_id}: planted.lines must be [start, end] with 1 <= start <= end")
    if not isinstance(planted["defect"], str) or not planted["defect"].strip():
        raise ValueError(f"{case_id}: planted.defect must be a non-empty sentence")
    if planted["dimension"] not in DIMENSIONS:
        raise ValueError(f"{case_id}: planted.dimension must be one of {sorted(DIMENSIONS)}")


@dataclass(frozen=True)
class Finding:
    severity: str
    title: str
    location: str
    details: str

    @property
    def paths(self) -> tuple[str, ...]:
        return location_paths(self.location)


def location_paths(location: str) -> tuple[str, ...]:
    """Every file path a location names: link text, plain ``path:line``, and the path inside a blob URL."""
    paths = [match.group(1) for match in _BLOB_URL.finditer(location)]
    rest = _URL.sub(" ", _BLOB_URL.sub(" ", location))
    paths += [match.group(0) for match in _FILE_PATH.finditer(rest)]
    return tuple(dict.fromkeys(path.strip("./") for path in paths))


def names_file(path: str, file: str) -> bool:
    """Whether a reported path names ``file``; generous on purpose, since the judge decides what a finding says."""
    return path == file or path.endswith(f"/{file}") or file.endswith(f"/{path}")


def _severity(heading: str) -> str | None:
    match = _SEVERITY_WORD.search(heading)
    return match.group(1).capitalize() if match else None


def _split_entry(text: str) -> tuple[str, str]:
    text = text.lstrip("* ")
    parts = _TITLE_SPLIT.split(text, maxsplit=1) if "**" in text else _DASH_SPLIT.split(text, maxsplit=1)
    title = parts[0].strip(" *")
    return title, parts[1].strip() if len(parts) > 1 else ""


def parse_report(report: str) -> list[Finding]:
    """The findings of a ``/code-review`` report, by severity section, in report order."""
    findings: list[Finding] = []
    severity: str | None = None
    entry: tuple[str, str] | None = None
    details: list[str] = []
    in_details = False

    def flush() -> None:
        if entry is None or severity is None:
            return
        body = _SUMMARY_LABEL.sub("", _DETAILS_TAG.sub("", "\n".join(details))).strip()
        location = entry[1]
        if not location_paths(location):
            location = next((match.group(1) for line in details if (match := _LOCATION_LINE.match(line))), location)
        findings.append(Finding(severity=severity, title=entry[0], location=location, details=body))

    for line in report.splitlines():
        if in_details or "<details>" in line:
            in_details = "</details>" not in line
            if entry is not None:
                details.append(line)
        elif heading := _HEADING.match(line):
            flush()
            entry, details = None, []
            severity = _severity(heading.group(1))
        elif severity is not None and (match := _ENTRY.match(line)):
            flush()
            entry, details = _split_entry(match.group(1)), []
        elif entry is not None:
            details.append(line)
    flush()
    return findings


def is_review_report(report: str) -> bool:
    return bool(_REVIEW_HEADING.search(report)) or bool(_NO_FINDINGS.search(report))


def is_degraded(report: str) -> bool:
    """Whether some detectors did not report: the review names them on a ``Review unavailable for`` line."""
    return bool(_DEGRADED.search(report))


def severity_counts(findings: Sequence[Finding]) -> dict[str, int]:
    counts = Counter(finding.severity for finding in findings)
    return {severity: counts[severity] for severity in SEVERITIES}


def blocking(findings: Sequence[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.severity in BLOCKING]


def clean_case_violation(report: str, findings: Sequence[Finding]) -> str | None:
    """Why a clean twin's run fails, or ``None``; it fails closed, passing only a complete, readable review.

    The run must end on a review report, with at least one parsed finding or a ``No findings`` line, no detector
    reported unavailable, and no Critical or Important finding.
    """
    if not is_review_report(report):
        return f"the run did not end on a code-review report: {report[-400:]!r}"
    if not findings and not _NO_FINDINGS.search(report):
        return f"the report has no parsed finding and no 'No findings' line: {report[-400:]!r}"
    if degraded := _DEGRADED.search(report):
        return f"the review is degraded: {degraded.group(0).strip()!r}"
    if found := blocking(findings):
        return f"the clean change got {len(found)} Critical/Important finding(s): {[f.title for f in found]}"
    return None


def located_in(finding: Finding, file: str) -> bool:
    """Whether a finding (not a question) is located in ``file``: only those go to the judge."""
    return finding.severity != "Question" and any(names_file(path, file) for path in finding.paths)


@dataclass(frozen=True)
class BugGrade:
    hit: bool
    noise: int
    explanations: tuple[str, ...]


@cache
def _judge():
    from automation.agent.base import BaseAgent, ThinkingLevel

    from .utils import MEMORY_JUDGE_MODEL

    return BaseAgent.get_model(model=MEMORY_JUDGE_MODEL, thinking_level=ThinkingLevel.MEDIUM)


async def judge_planted_bug(defect: str, finding: Finding) -> Verdict:
    """One judge call: does this finding describe the planted defect?"""
    from .evaluators import Verdict

    prompt = (
        "A change is known to contain this defect:\n\n"
        f"{defect}\n\n"
        "A code reviewer reported the finding below on that change.\n\n"
        f"Severity: {finding.severity}\nTitle: {finding.title}\nLocation: {finding.location}\n\n{finding.details}\n\n"
        "Pass only if the finding identifies the same defect: the same root cause in the same code, in any wording "
        "and with any proposed fix. A finding about a different problem in the same code does not pass."
    )
    result = await _judge().with_structured_output(Verdict).ainvoke(prompt)
    return result or Verdict(passed=False, explanation="the judge returned nothing")


async def grade_bug_case(findings: Sequence[Finding], planted: dict) -> BugGrade:
    """A hit when the judge matches any finding in the planted file; noise is every other Critical/Important."""
    matched: set[int] = set()
    explanations = []
    for index, finding in enumerate(findings):
        if not located_in(finding, planted["file"]):
            continue
        verdict = await judge_planted_bug(planted["defect"], finding)
        explanations.append(f"{finding.title}: {verdict.explanation}")
        if verdict.passed:
            matched.add(index)
    noise = sum(1 for index, finding in enumerate(findings) if finding.severity in BLOCKING and index not in matched)
    return BugGrade(hit=bool(matched), noise=noise, explanations=tuple(explanations))
