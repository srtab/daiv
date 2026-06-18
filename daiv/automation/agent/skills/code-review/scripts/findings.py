#!/usr/bin/env python3
"""Finding helpers for the code-review skill (detector fan-out + verify).

Subcommand:
  merge   validate + cross-detector dedup detector output files passed by path

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
# Dedup precedence: the higher rank wins when two findings collapse to one key. Declared EXPLICITLY
# (not derived from the BARS enum's array order) so reordering the enum in finding.schema.json can't
# silently invert severity. Kept out of the schema file itself because that schema is also embedded as
# the detectors' structured-output response_format, which must stay a clean JSON-Schema subset — so the
# rank lives here and is coverage-checked against BARS (a new bar must get an explicit rank, below).
_BAR_RANK = {"defect": 3, "structural": 2, "question": 1}
if set(_BAR_RANK) != set(BARS):
    raise ValueError(f"_BAR_RANK must cover exactly the bar enum {BARS}; got {tuple(_BAR_RANK)}")


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


# Prose archetypes are catch-alls (review-workflow.md: "discussion for everything else").
# Two genuinely distinct findings from different detectors legitimately share one
# (file, line, "discussion"|"question") — e.g. a security concern and a custom-rules
# violation on the same line — so collapsing them on (file, line, archetype) alone would
# silently drop one (and a custom-rules `source` with it). Key those on `detector` too so
# they survive; downstream they demote to the summary, where both are visible. The four
# inline fix archetypes keep the bare (file, line, archetype) key: there, same line + same
# archetype means the same concrete fix, so collapsing genuine cross-detector duplicates
# (strongest bar wins) is correct, and delivery's anchor dedup catches any that reach posting.
_PROSE_ARCHETYPES = frozenset({"discussion", "question"})


def _key(finding: dict) -> tuple:
    base = (finding["file"], finding["line"], finding["archetype"])
    if finding["archetype"] in _PROSE_ARCHETYPES:
        return (*base, finding["detector"])
    return base


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

    Drops malformed findings, then dedupes survivors on the ``(file, line, archetype)`` key
    (prose archetypes — ``discussion`` / ``question`` — key on ``detector`` as well; see ``_key``),
    keeping the strongest ``bar`` per key. Returns ``{"findings", "candidates", "dropped", "merged"}``:

    - ``findings`` — the deduped, schema-valid findings the review carries into adversarial
      verification (Stage 2) and delivery.
    - ``candidates`` — count of distinct findings after dedup (== ``len(findings)``); the
      pre-refutation total the skill reports in its Step 7 status line.
    - ``dropped`` — count of findings that failed schema validation (a detector emitting
      invalid findings is a real signal worth surfacing, not hiding).
    - ``merged`` — findings absorbed by the dedup: the surplus beyond the one kept per key,
      so two findings collapsing into one is ``merged: 1``.

    Note this is the pre-delivery cross-detector merge; the separate delivery dedup against
    already-posted notes (``gitlab-delivery.md`` Step 4) is anchor-based on
    ``(kind, archetype, file, anchor)``.
    """
    valid, dropped = validate(raw)
    deduped = dedupe(valid)
    return {"findings": deduped, "candidates": len(deduped), "dropped": dropped, "merged": len(valid) - len(deduped)}


def read_findings_from_files(paths: list) -> list:
    """Read raw findings from detector output files.

    Each path is a detector's ``{"findings": [...]}`` object (a bare ``[...]`` array is tolerated).
    A missing or unparseable file is skipped with a stderr note — one absent or corrupt detector
    output must not abort the whole merge.
    """
    raw: list = []
    for path in paths:
        try:
            with Path(path).open(encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            sys.stderr.write(f"skipping missing findings file: {path}\n")
            continue
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"skipping unreadable findings file {path}: {exc}\n")
            continue
        items = data.get("findings", []) if isinstance(data, dict) else data
        if isinstance(items, list):
            raw.extend(items)
        else:
            sys.stderr.write(f"skipping findings file {path}: no 'findings' array\n")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    merge_parser = sub.add_parser("merge", help="Validate + cross-detector dedup detector output files.")
    merge_parser.add_argument(
        "paths", nargs="*", help='Detector output JSON files (each a {"findings": [...]} object).'
    )
    args = parser.parse_args()

    if args.cmd == "merge":
        json.dump(merge(read_findings_from_files(args.paths)), sys.stdout)
        sys.stdout.write("\n")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
