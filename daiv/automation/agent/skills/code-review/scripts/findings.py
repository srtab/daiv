#!/usr/bin/env python3
"""Finding helpers for the code-review skill (detector fan-out + verify).

Subcommand:
  merge   validate + cross-detector dedup a JSON array of raw findings on stdin

The detectors (dispatched as subagents) emit findings as JSON; this script owns
the deterministic parts — schema validation and cross-detector dedup — so the
same raw findings always reduce to the same posting set. Delivery dedup (vs
already-posted notes) stays in marker.py; this is the pre-delivery merge.
"""
# ruff: NOQA: T201

import argparse
import json
import sys

DETECTORS = ("correctness", "security", "performance", "structure", "custom-rules")
BARS = ("defect", "structural", "question")
ARCHETYPES = (
    "remove_dead_lines",
    "use_framework_idiom",
    "replace_with_constant",
    "swap_library_call",
    "question",
    "discussion",
)
REQUIRED_FIELDS = ("detector", "file", "line", "bar", "archetype", "title", "rationale")
_BAR_RANK = {"defect": 3, "structural": 2, "question": 1}


def is_valid(finding: object) -> bool:
    if not isinstance(finding, dict):
        return False
    if any(finding.get(k) in (None, "") for k in REQUIRED_FIELDS):
        return False
    if finding["detector"] not in DETECTORS:
        return False
    if finding["bar"] not in BARS:
        return False
    if finding["archetype"] not in ARCHETYPES:
        return False
    line = finding["line"]
    return not isinstance(line, bool) and isinstance(line, int) and line >= 1


def validate(findings: list) -> tuple[list, int]:
    valid = [f for f in findings if is_valid(f)]
    return valid, len(findings) - len(valid)


def _key(finding: dict) -> tuple:
    return (finding["file"], finding["line"], finding["archetype"])


def dedupe(findings: list) -> list:
    best: dict[tuple, dict] = {}
    order: list[tuple] = []
    for f in findings:
        k = _key(f)
        if k not in best:
            order.append(k)
            best[k] = f
        elif _BAR_RANK[f["bar"]] > _BAR_RANK[best[k]["bar"]]:
            best[k] = f
    return [best[k] for k in order]


def merge(raw: list) -> dict:
    valid, dropped = validate(raw)
    deduped = dedupe(valid)
    return {"findings": deduped, "candidates": len(deduped), "dropped": dropped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("merge", help="Validate + cross-detector dedup raw findings (JSON array on stdin).")
    args = parser.parse_args()

    if args.cmd == "merge":
        try:
            raw = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"invalid JSON on stdin: {exc}\n")
            return 1
        if not isinstance(raw, list):
            sys.stderr.write("expected a JSON array of findings on stdin\n")
            return 1
        json.dump(merge(raw), sys.stdout)
        sys.stdout.write("\n")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
