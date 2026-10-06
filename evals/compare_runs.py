"""Compare two eval-metrics files written by ``make eval-prompts`` and print a markdown verdict for the PR body.

Each JSONL row is one test item on one pass (``run``). A case's result is the majority of its rows; a FAIL→PASS flip
counts as a gain only when both sides are unanimous over at least ``STABLE_VOTES`` votes, while any PASS→FAIL majority
flip is a regression. Only suites present in both files are compared, so a before run over every suite can serve a PR
that re-ran a subset.

Usage: uv run evals/compare_runs.py BEFORE.jsonl AFTER.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

STABLE_VOTES = 3
TOKEN_GAIN = 0.05

Verdict = Literal["regressed", "improved", "neutral"]


@dataclass(frozen=True)
class Votes:
    passes: int
    total: int

    @property
    def majority(self) -> bool:
        return self.passes * 2 > self.total

    @property
    def unanimous(self) -> bool:
        return self.total >= STABLE_VOTES and self.passes in {0, self.total}

    def __str__(self) -> str:
        return f"{'PASS' if self.majority else 'FAIL'} {self.passes}/{self.total}"


@dataclass(frozen=True)
class CaseDelta:
    nodeid: str
    case: str
    suite: str
    before: Votes
    after: Votes

    @property
    def regressed(self) -> bool:
        return self.before.majority and not self.after.majority

    @property
    def stable_gain(self) -> bool:
        return not self.before.majority and self.after.majority and self.before.unanimous and self.after.unanimous

    @property
    def flip(self) -> str:
        if self.regressed:
            return "PASS→FAIL"
        if self.stable_gain:
            return "FAIL→PASS"
        if not self.before.majority and self.after.majority:
            return "FAIL→PASS (unstable, not a gain)"
        return ""


@dataclass(frozen=True)
class Comparison:
    deltas: list[CaseDelta]
    missing: list[str]
    one_sided_suites: list[str]
    before_medians: dict[str, dict[str, float | None]]
    after_medians: dict[str, dict[str, float | None]]
    before_recall: dict[str, float] | None
    after_recall: dict[str, float] | None
    warnings: list[str]
    verdict: Verdict


def load_rows(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as err:
            raise SystemExit(f"{path}:{number}: not a JSON row ({err})") from err
    return rows


def case_votes(rows: list[dict]) -> dict[str, Votes]:
    outcomes: dict[str, list[bool]] = {}
    for row in rows:
        outcomes.setdefault(row["nodeid"], []).append(bool(row["passed"]))
    return {nodeid: Votes(sum(results), len(results)) for nodeid, results in outcomes.items()}


def suite_medians(rows: list[dict]) -> dict[str, dict[str, float | None]]:
    by_suite: dict[str, list[dict]] = {}
    for row in rows:
        by_suite.setdefault(row["suite"], []).append(row)
    return {
        suite: {
            "input_tokens": _median(suite_rows, "input_tokens"),
            "output_tokens": _median(suite_rows, "output_tokens"),
            "turns": _median(suite_rows, "turns"),
            "cache_read_share": _cache_read_share(suite_rows),
        }
        for suite, suite_rows in by_suite.items()
    }


def _median(rows: list[dict], key: str) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    return statistics.median(values) if values else None


def _cache_read_share(rows: list[dict]) -> float | None:
    measured = [row for row in rows if row.get("input_tokens")]
    total = sum(row["input_tokens"] for row in measured)
    return sum(row.get("cache_read_tokens") or 0 for row in measured) / total if total else None


def token_gain(before: dict[str, dict[str, float | None]], after: dict[str, dict[str, float | None]]) -> bool:
    """Median input tokens per run down by ``TOKEN_GAIN`` or more in some suite, and up by that much in none."""
    changes = []
    for suite in before.keys() & after.keys():
        old, new = before[suite]["input_tokens"], after[suite]["input_tokens"]
        if old and new is not None:
            changes.append((new - old) / old)
    return bool(changes) and min(changes) <= -TOKEN_GAIN and max(changes) < TOKEN_GAIN


def recall_summary(rows: list[dict]) -> dict[str, float] | None:
    """Hits, clean-twin passes and noise for the code-review recall suite, whose rows carry a ``kind``."""
    review = [row for row in rows if "kind" in row]
    if not review:
        return None
    votes = case_votes(review)
    kinds = {row["nodeid"]: row["kind"] for row in review}
    runs = len({row["run"] for row in review})
    return {
        "hits": sum(1 for nodeid, vote in votes.items() if kinds[nodeid] == "bug" and vote.majority),
        "bug_cases": sum(1 for kind in kinds.values() if kind == "bug"),
        "clean_passes": sum(1 for nodeid, vote in votes.items() if kinds[nodeid] == "clean" and vote.majority),
        "clean_cases": sum(1 for kind in kinds.values() if kind == "clean"),
        "noise_per_run": sum(row.get("noise") or 0 for row in review if row["kind"] == "bug") / runs,
        "clean_findings_per_run": sum(row.get("noise") or 0 for row in review if row["kind"] == "clean") / runs,
    }


def _warnings(label: str, rows: list[dict]) -> list[str]:
    warnings = []
    if len(shas := sorted({str(row.get("git_sha")) for row in rows})) > 1:
        warnings.append(
            f"{label} mixes rows from {len(shas)} commits ({', '.join(shas)}); use a fresh OUT file per run."
        )
    return warnings


def compare(before_rows: list[dict], after_rows: list[dict]) -> Comparison:
    before_suites = {row["suite"] for row in before_rows}
    after_suites = {row["suite"] for row in after_rows}
    shared = before_suites & after_suites
    before_rows = [row for row in before_rows if row["suite"] in shared]
    after_rows = [row for row in after_rows if row["suite"] in shared]

    before_votes, after_votes = case_votes(before_rows), case_votes(after_rows)
    labels = {row["nodeid"]: (row["case"], row["suite"]) for row in [*before_rows, *after_rows]}
    deltas = [
        CaseDelta(nodeid, *labels[nodeid], before_votes[nodeid], after_votes[nodeid])
        for nodeid in sorted(before_votes.keys() & after_votes.keys())
    ]
    missing = sorted(before_votes.keys() ^ after_votes.keys())

    warnings = _warnings("BEFORE", before_rows) + _warnings("AFTER", after_rows)
    before_models = {row.get("model") for row in before_rows}
    after_models = {row.get("model") for row in after_rows}
    if before_models != after_models:
        warnings.append(f"BEFORE ran {sorted(map(str, before_models))} but AFTER ran {sorted(map(str, after_models))}.")

    before_medians, after_medians = suite_medians(before_rows), suite_medians(after_rows)
    if any(delta.regressed for delta in deltas):
        verdict: Verdict = "regressed"
    elif any(delta.stable_gain for delta in deltas) or token_gain(before_medians, after_medians):
        verdict = "improved"
    else:
        verdict = "neutral"

    return Comparison(
        deltas=deltas,
        missing=missing,
        one_sided_suites=sorted(before_suites ^ after_suites),
        before_medians=before_medians,
        after_medians=after_medians,
        before_recall=recall_summary(before_rows),
        after_recall=recall_summary(after_rows),
        warnings=warnings,
        verdict=verdict,
    )


def _number(value: float | None) -> str:
    return "–" if value is None else f"{value:,.0f}"


def _change(old: float | None, new: float | None) -> str:
    if old is None or new is None:
        return f"{_number(old)} → {_number(new)}"
    percent = f" ({(new - old) / old:+.1%})" if old else ""
    return f"{_number(old)} → {_number(new)}{percent}"


def _share(value: float | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def render_markdown(comparison: Comparison) -> str:
    regressions = sum(delta.regressed for delta in comparison.deltas)
    gains = sum(delta.stable_gain for delta in comparison.deltas)
    lines = [
        "## Eval comparison",
        "",
        f"**Verdict: {comparison.verdict}** — {regressions} PASS→FAIL flip(s), {gains} stable FAIL→PASS flip(s).",
        "",
        "| Case | Suite | Before | After | Flip |",
        "|---|---|---|---|---|",
        *(
            f"| `{delta.case}` | {delta.suite} | {delta.before} | {delta.after} | {delta.flip} |"
            for delta in comparison.deltas
        ),
        "",
        "| Suite | Input tokens / run (median) | Output tokens / run (median) | Turns / run (median) "
        "| Cache-read share |",
        "|---|---|---|---|---|",
    ]
    for suite in sorted(comparison.before_medians.keys() & comparison.after_medians.keys()):
        old, new = comparison.before_medians[suite], comparison.after_medians[suite]
        lines.append(
            f"| {suite} | {_change(old['input_tokens'], new['input_tokens'])} "
            f"| {_change(old['output_tokens'], new['output_tokens'])} | {_change(old['turns'], new['turns'])} "
            f"| {_share(old['cache_read_share'])} → {_share(new['cache_read_share'])} |"
        )
    if comparison.before_recall and comparison.after_recall:
        old, new = comparison.before_recall, comparison.after_recall
        lines += [
            "",
            "### Code review recall",
            "",
            "| | Before | After |",
            "|---|---|---|",
            f"| Bug cases hit (majority) | {old['hits']:.0f}/{old['bug_cases']:.0f} "
            f"| {new['hits']:.0f}/{new['bug_cases']:.0f} |",
            f"| Clean twins passing (majority) | {old['clean_passes']:.0f}/{old['clean_cases']:.0f} "
            f"| {new['clean_passes']:.0f}/{new['clean_cases']:.0f} |",
            f"| Noise per run (Critical/Important on bug cases, not the planted bug) | {old['noise_per_run']:.1f} "
            f"| {new['noise_per_run']:.1f} |",
            f"| Critical/Important per run on clean twins | {old['clean_findings_per_run']:.1f} "
            f"| {new['clean_findings_per_run']:.1f} |",
        ]
    if comparison.missing:
        lines += ["", "Not compared (rows on one side only):", *(f"- `{nodeid}`" for nodeid in comparison.missing)]
    if comparison.one_sided_suites:
        lines += ["", f"Suites run on one side only (ignored): {', '.join(comparison.one_sided_suites)}"]
    if comparison.warnings:
        lines += ["", *(f"> **Warning:** {warning}" for warning in comparison.warnings)]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    sys.stdout.write(render_markdown(compare(load_rows(args.before), load_rows(args.after))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
