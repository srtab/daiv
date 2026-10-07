"""Compare two eval-metrics files written by ``make eval-prompts`` and print a markdown verdict for the PR body.

Each JSONL row is one test item on one pass (``run``). A case's result is the majority of its rows. Two kinds of flip
can decide a verdict: a PASS→FAIL majority flip, and a FAIL→PASS flip with both sides unanimous over at least
``STABLE_VOTES`` votes. Three votes cannot tell such a flip from run-to-run variance, so each one waits for
confirmation: the flipped cases are re-run on both commits into two confirmation files (``--confirm``), and the flip
counts — as a regression or a gain — only if the majority over at least ``CONFIRM_VOTES`` votes per side still flips.
Confirmation rows add votes and nothing else; token, turn and noise figures come from the main files, and tokens never
decide the verdict.

A row with ``passed: null`` is a pass that cast no vote (a skip, or a failure before the first model call); a case with
fewer votes than its file has passes makes the verdict inconclusive. Only suites present in both files are compared, so
a before run over every suite can serve a PR that re-ran a subset.

Usage: uv run evals/compare_runs.py BEFORE.jsonl AFTER.jsonl [--confirm BEFORE_CONFIRM.jsonl AFTER_CONFIRM.jsonl]
(exits 1 when no case ran on both sides, or the verdict is unconfirmed or inconclusive)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

STABLE_VOTES = 3
CONFIRM_VOTES = 9

Verdict = Literal["regressed", "unconfirmed", "inconclusive", "improved", "neutral"]


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
    """One case's votes on each side: from the main files, and with the confirmation re-runs added (``*_all``)."""

    nodeid: str
    case: str
    suite: str
    before: Votes
    after: Votes
    before_all: Votes
    after_all: Votes

    @property
    def _dropped(self) -> bool:
        return self.before.majority and not self.after.majority

    @property
    def _stable_rise(self) -> bool:
        return not self.before.majority and self.after.majority and self.before.unanimous and self.after.unanimous

    @property
    def _confirmed(self) -> bool:
        return min(self.before_all.total, self.after_all.total) >= CONFIRM_VOTES

    @property
    def _holds(self) -> bool:
        return self.before_all.majority == self.before.majority and self.after_all.majority == self.after.majority

    @property
    def needs_confirmation(self) -> bool:
        return (self._dropped or self._stable_rise) and not self._confirmed

    @property
    def regressed(self) -> bool:
        return self._dropped and self._confirmed and self._holds

    @property
    def stable_gain(self) -> bool:
        return self._stable_rise and self._confirmed and self._holds

    @property
    def flip(self) -> str:
        if not (self._dropped or self._stable_rise):
            return "FAIL→PASS (unstable, not a gain)" if self.after.majority and not self.before.majority else ""
        direction = "PASS→FAIL" if self._dropped else "FAIL→PASS"
        if not self._confirmed:
            return f"{direction}, needs confirmation"
        return direction if self._holds else f"{direction} did not hold"


@dataclass(frozen=True)
class Comparison:
    deltas: list[CaseDelta]
    incomplete: dict[str, str]
    missing: list[str]
    one_sided_suites: list[str]
    before_run: str
    after_run: str
    before_medians: dict[str, dict[str, float | None]]
    after_medians: dict[str, dict[str, float | None]]
    largest_case_increase: dict[str, tuple[str, float, float] | None]
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


def _voting(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["passed"] is not None]


def _passes(rows: list[dict]) -> int:
    return len({row["run"] for row in rows})


def case_votes(rows: list[dict]) -> dict[str, Votes]:
    outcomes: dict[str, list[bool]] = {}
    for row in _voting(rows):
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


def largest_case_increase(
    before_rows: list[dict], after_rows: list[dict], labels: dict[str, tuple[str, str]]
) -> dict[str, tuple[str, float, float] | None]:
    """Per suite, the case whose median input tokens rose most in relative terms, as ``(case, before, after)``."""
    before_by_case, after_by_case = _case_medians(before_rows), _case_medians(after_rows)
    largest: dict[str, tuple[str, float, float] | None] = {}
    for nodeid in sorted(before_by_case.keys() & after_by_case.keys()):
        case, suite = labels[nodeid]
        old, new = before_by_case[nodeid], after_by_case[nodeid]
        best = largest.setdefault(suite, None)
        if old and new is not None and new > old and (best is None or new / old > best[2] / best[1]):
            largest[suite] = (case, old, new)
    return largest


def _case_medians(rows: list[dict]) -> dict[str, float | None]:
    by_case: dict[str, list[dict]] = {}
    for row in rows:
        by_case.setdefault(row["nodeid"], []).append(row)
    return {nodeid: _median(case_rows, "input_tokens") for nodeid, case_rows in by_case.items()}


def recall_summary(rows: list[dict], votes: dict[str, Votes] | None = None) -> dict[str, float] | None:
    """Hits, clean-twin passes and noise for the code-review recall suite, whose rows carry a ``kind``.

    ``votes`` overrides the votes counted from ``rows``, so hits can include confirmation re-runs while noise stays a
    per-run figure over ``rows``.
    """
    review = [row for row in rows if "kind" in row]
    if not review:
        return None
    votes = votes if votes is not None else case_votes(review)
    kinds = {row["nodeid"]: row["kind"] for row in review}
    runs = _passes(review)
    return {
        "hits": sum(1 for nodeid, vote in votes.items() if kinds.get(nodeid) == "bug" and vote.majority),
        "bug_cases": sum(1 for kind in kinds.values() if kind == "bug"),
        "clean_passes": sum(1 for nodeid, vote in votes.items() if kinds.get(nodeid) == "clean" and vote.majority),
        "clean_cases": sum(1 for kind in kinds.values() if kind == "clean"),
        "noise_per_run": sum(row.get("noise") or 0 for row in review if row["kind"] == "bug") / runs,
        "clean_findings_per_run": sum(row.get("noise") or 0 for row in review if row["kind"] == "clean") / runs,
    }


def _incomplete(nodeids: set[str], before_rows: list[dict], after_rows: list[dict]) -> dict[str, str]:
    """Cases on both sides with fewer votes than their file has passes, each with its vote counts."""
    before_passes, after_passes = _passes(before_rows), _passes(after_rows)
    before = Counter(row["nodeid"] for row in _voting(before_rows))
    after = Counter(row["nodeid"] for row in _voting(after_rows))
    return {
        nodeid: f"BEFORE {before[nodeid]} of {before_passes} votes, AFTER {after[nodeid]} of {after_passes}"
        for nodeid in sorted(nodeids)
        if before[nodeid] < before_passes or after[nodeid] < after_passes
    }


def _warnings(label: str, rows: list[dict]) -> list[str]:
    warnings = []
    if len(shas := sorted({str(row.get("git_sha")) for row in rows})) > 1:
        warnings.append(
            f"{label} mixes rows from {len(shas)} commits ({', '.join(shas)}); use a fresh OUT file per run."
        )
    pairs = [(row["nodeid"], row["run"]) for row in rows]
    repeated = len(pairs) - len(set(pairs))
    if repeated > 0:
        warnings.append(f"{label} has {repeated} repeated (case, run) rows; use a fresh OUT file per run.")
    return warnings


def _commits(rows: list[dict]) -> set[str]:
    return {row["git_sha"] for row in rows if row.get("git_sha")}


def _run_description(rows: list[dict]) -> str:
    commits = ", ".join(sorted({str(row.get("git_sha")) for row in rows})) or "–"
    models = ", ".join(sorted({str(row.get("model")) for row in rows})) or "–"
    return f"{commits} on {models}"


def _confirmation_warnings(label: str, confirm_rows: list[dict], rows: list[dict]) -> list[str]:
    warnings = _warnings(f"{label} confirmation", confirm_rows)
    if confirm_rows and (elsewhere := _commits(confirm_rows) - _commits(rows)):
        warnings.append(
            f"{label} confirmation ran on {', '.join(sorted(elsewhere))}, "
            f"not on {label}'s {', '.join(sorted(_commits(rows)))}."
        )
    return warnings


def compare(
    before_rows: list[dict],
    after_rows: list[dict],
    *,
    confirm_before: list[dict] | None = None,
    confirm_after: list[dict] | None = None,
) -> Comparison:
    confirm_before, confirm_after = confirm_before or [], confirm_after or []
    before_suites = {row["suite"] for row in before_rows}
    after_suites = {row["suite"] for row in after_rows}
    shared = before_suites & after_suites
    before_rows = [row for row in before_rows if row["suite"] in shared]
    after_rows = [row for row in after_rows if row["suite"] in shared]

    before_votes, after_votes = case_votes(before_rows), case_votes(after_rows)
    before_all = case_votes([*before_rows, *confirm_before])
    after_all = case_votes([*after_rows, *confirm_after])
    labels = {row["nodeid"]: (row["case"], row["suite"]) for row in [*before_rows, *after_rows]}
    voted_on_both = before_votes.keys() & after_votes.keys()
    deltas = [
        CaseDelta(
            nodeid, *labels[nodeid], before_votes[nodeid], after_votes[nodeid], before_all[nodeid], after_all[nodeid]
        )
        for nodeid in sorted(voted_on_both)
    ]
    before_cases, after_cases = {row["nodeid"] for row in before_rows}, {row["nodeid"] for row in after_rows}
    missing = sorted(before_cases ^ after_cases)
    incomplete = _incomplete(before_cases & after_cases, before_rows, after_rows)
    before_rows_shared = [row for row in _voting(before_rows) if row["nodeid"] in voted_on_both]
    after_rows_shared = [row for row in _voting(after_rows) if row["nodeid"] in voted_on_both]

    warnings = _warnings("BEFORE", before_rows) + _warnings("AFTER", after_rows)
    warnings += _confirmation_warnings("BEFORE", confirm_before, before_rows)
    warnings += _confirmation_warnings("AFTER", confirm_after, after_rows)
    before_models = {row.get("model") for row in before_rows}
    after_models = {row.get("model") for row in after_rows}
    if before_models != after_models:
        warnings.append(f"BEFORE ran {sorted(map(str, before_models))} but AFTER ran {sorted(map(str, after_models))}.")
    if not deltas:
        warnings.append("No case ran on both sides; this comparison measured nothing.")
    if missing:
        warnings.append(
            f"{len(missing)} case(s) ran on one side only (see Not compared); was a case added, renamed or removed?"
        )
    warnings += [
        f"BEFORE and AFTER share commit {sha}; was the AFTER run on the PR branch?"
        for sha in sorted(_commits(before_rows) & _commits(after_rows))
    ]
    if zero_token_rows := sum(row.get("input_tokens") == 0 for row in _voting([*before_rows, *after_rows])):
        warnings.append(
            f"{zero_token_rows} row(s) report 0 input tokens; usage may be unreported, which hides failing votes."
        )

    before_medians, after_medians = suite_medians(before_rows_shared), suite_medians(after_rows_shared)
    if any(delta.regressed for delta in deltas):
        verdict: Verdict = "regressed"
    elif any(delta.needs_confirmation for delta in deltas):
        verdict = "unconfirmed"
    elif incomplete:
        verdict = "inconclusive"
    elif any(delta.stable_gain for delta in deltas):
        verdict = "improved"
    else:
        verdict = "neutral"

    return Comparison(
        deltas=deltas,
        incomplete=incomplete,
        missing=missing,
        one_sided_suites=sorted(before_suites ^ after_suites),
        before_run=_run_description(before_rows),
        after_run=_run_description(after_rows),
        before_medians=before_medians,
        after_medians=after_medians,
        largest_case_increase=largest_case_increase(before_rows_shared, after_rows_shared, labels),
        before_recall=recall_summary(before_rows_shared, before_all),
        after_recall=recall_summary(after_rows_shared, after_all),
        warnings=warnings,
        verdict=verdict,
    )


def _number(value: float | None, decimals: int = 0) -> str:
    return "–" if value is None else f"{value:,.{decimals}f}"


def _change(old: float | None, new: float | None, decimals: int = 0) -> str:
    if old is None or new is None:
        return f"{_number(old, decimals)} → {_number(new, decimals)}"
    percent = f" ({(new - old) / old:+.1%})" if old else ""
    return f"{_number(old, decimals)} → {_number(new, decimals)}{percent}"


def _share(value: float | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def _largest_increase(increase: tuple[str, float, float] | None) -> str:
    return "–" if increase is None else f"`{increase[0]}` {_change(increase[1], increase[2])}"


def _votes(initial: Votes, combined: Votes) -> str:
    return str(initial) if combined == initial else f"{initial} ({combined} with re-runs)"


def render_markdown(comparison: Comparison) -> str:
    regressions = sum(delta.regressed for delta in comparison.deltas)
    gains = sum(delta.stable_gain for delta in comparison.deltas)
    pending = [delta for delta in comparison.deltas if delta.needs_confirmation]
    lines = [
        "## Eval comparison",
        "",
        f"BEFORE: {comparison.before_run} · AFTER: {comparison.after_run}",
        "",
        f"**Verdict: {comparison.verdict}** — {regressions} PASS→FAIL flip(s), {gains} stable FAIL→PASS flip(s), "
        f"{len(pending)} flip(s) awaiting confirmation.",
        "",
        "| Case | Suite | Before | After | Flip |",
        "|---|---|---|---|---|",
        *(
            f"| `{delta.case}` | {delta.suite} | {_votes(delta.before, delta.before_all)} "
            f"| {_votes(delta.after, delta.after_all)} | {delta.flip} |"
            for delta in comparison.deltas
        ),
        "",
        "| Suite | Input tokens / run (median) | Output tokens / run (median) | Turns / run (median) "
        "| Cache-read share | Largest case increase |",
        "|---|---|---|---|---|---|",
    ]
    for suite in sorted(comparison.before_medians.keys() & comparison.after_medians.keys()):
        old, new = comparison.before_medians[suite], comparison.after_medians[suite]
        lines.append(
            f"| {suite} | {_change(old['input_tokens'], new['input_tokens'])} "
            f"| {_change(old['output_tokens'], new['output_tokens'])} "
            f"| {_change(old['turns'], new['turns'], decimals=1)} "
            f"| {_share(old['cache_read_share'])} → {_share(new['cache_read_share'])} "
            f"| {_largest_increase(comparison.largest_case_increase.get(suite))} |"
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
    if pending:
        repeats = max(CONFIRM_VOTES - min(delta.before.total, delta.after.total) for delta in pending)
        cases = " ".join(delta.nodeid for delta in pending)
        lines += [
            "",
            "Needs confirmation: re-run these cases on each side's commit, each into a fresh file, then pass both "
            "files with `--confirm BEFORE_CONFIRM AFTER_CONFIRM`:",
            "",
            f'`DAIV_EVAL_REPEATS={repeats} make eval-prompts CASES="{cases}" OUT=<fresh file>`',
        ]
    if comparison.incomplete:
        lines += [
            "",
            "Incomplete (a pass cast no vote: a skip, or a failure before the first model call):",
            *(f"- `{nodeid}`: {votes}" for nodeid, votes in comparison.incomplete.items()),
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
    parser.add_argument("--confirm", nargs=2, type=Path, metavar=("BEFORE_CONFIRM", "AFTER_CONFIRM"))
    args = parser.parse_args(argv)
    confirm_before, confirm_after = (load_rows(path) for path in args.confirm) if args.confirm else ([], [])
    comparison = compare(
        load_rows(args.before), load_rows(args.after), confirm_before=confirm_before, confirm_after=confirm_after
    )
    sys.stdout.write(render_markdown(comparison))
    return 0 if comparison.deltas and comparison.verdict not in {"inconclusive", "unconfirmed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
