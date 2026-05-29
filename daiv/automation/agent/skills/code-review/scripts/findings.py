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
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parent / "finding.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_PROPS = _SCHEMA["properties"]

DETECTORS = tuple(_PROPS["detector"]["enum"])
BARS = tuple(_PROPS["bar"]["enum"])
ARCHETYPES = tuple(_PROPS["archetype"]["enum"])
REQUIRED_FIELDS = tuple(_SCHEMA["required"])
# reversed(BARS): the schema lists bars highest-severity-first, so reversed() makes rank 1 the lowest.
# Derived from BARS so a new bar can't be accepted by is_valid yet KeyError in dedupe.
_BAR_RANK = {bar: rank for rank, bar in enumerate(reversed(BARS), start=1)}


def is_valid(finding: object) -> bool:
    if not isinstance(finding, dict):
        return False
    for k in REQUIRED_FIELDS:
        value = finding.get(k)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    if finding["detector"] not in DETECTORS:
        return False
    if finding["bar"] not in BARS:
        return False
    if finding["archetype"] not in ARCHETYPES:
        return False
    if finding["detector"] == "custom-rules" and not finding.get("source"):
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
        elif _BAR_RANK.get(f["bar"], 0) > _BAR_RANK.get(best[k]["bar"], 0):
            best[k] = f
    return [best[k] for k in order]


def merge(raw: list) -> dict:
    """Validate findings against the schema and collapse cross-detector duplicates.

    Drops malformed findings, then dedupes survivors on the ``(file, line, archetype)`` key,
    keeping the strongest ``bar`` per key. Returns ``{"findings", "candidates", "dropped", "merged"}``:

    - ``findings`` — the deduped, schema-valid findings the review carries into adversarial
      verification (Stage 2) and delivery.
    - ``candidates`` — count of distinct findings after dedup (== ``len(findings)``); the
      pre-refutation total the skill reports in its Step 7 status line.
    - ``dropped`` — count of findings that failed schema validation (a detector emitting
      invalid findings is a real signal worth surfacing, not hiding).
    - ``merged`` — findings absorbed by the ``(file, line, archetype)`` dedup: the surplus
      beyond the one kept per key, so two findings collapsing into one is ``merged: 1``.

    Note this ``(file, line, archetype)`` dedup is the pre-delivery cross-detector merge; the
    separate delivery dedup against already-posted notes (Stage 3) is anchor-based on
    ``(kind, archetype, file, anchor)``.
    """
    valid, dropped = validate(raw)
    deduped = dedupe(valid)
    return {"findings": deduped, "candidates": len(deduped), "dropped": dropped, "merged": len(valid) - len(deduped)}


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
