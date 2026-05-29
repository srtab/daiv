#!/usr/bin/env python3
"""Rule-source resolver for the code-review skill (custom-rules detector).

Subcommand:
  resolve   locate per-repo review rule sources under a repo root and emit one
            source-labeled rules document for the custom-rules detector.

Precedence: .agents/review-rules.md is authoritative; the repo context file
(AGENTS.md by default) and .agents/AGENTS.md are supplementary, mined only for
diff-checkable conventions. ``has_rules=false`` tells the skill to skip the
custom-rules detector entirely. This reads ordinary repo files only — no config
or DB access.
"""
# ruff: NOQA: T201

import argparse
import json
import sys
from pathlib import Path

PRIMARY = ".agents/review-rules.md"
AGENTS_MEMORY = ".agents/AGENTS.md"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def resolve(root: str, context_file: str = "AGENTS.md") -> dict:
    base = Path(root)
    found: list[str] = []
    parts: list[str] = []

    primary_text = _read(base / PRIMARY)
    if primary_text:
        found.append(PRIMARY)
        parts.append(f"# Review rules (authoritative) — {PRIMARY}\n\n{primary_text}")

    seen = {PRIMARY}
    for rel in (context_file, AGENTS_MEMORY):
        if not rel or rel in seen:
            continue
        seen.add(rel)
        text = _read(base / rel)
        if text:
            found.append(rel)
            parts.append(f"# Supplementary conventions (mine only diff-checkable rules) — {rel}\n\n{text}")

    return {"has_rules": bool(found), "found": found, "rules_document": "\n\n---\n\n".join(parts)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("resolve", help="Locate + assemble per-repo review rule sources.")
    p.add_argument("--root", default=".", help="Repository root to search.")
    p.add_argument(
        "--context-file", default="AGENTS.md", help="Repo context file name (from .daiv.yml; default AGENTS.md)."
    )
    args = parser.parse_args()

    if args.cmd == "resolve":
        json.dump(resolve(args.root, args.context_file), sys.stdout)
        sys.stdout.write("\n")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
