"""Deterministic checks on the shape of a generated PR description.

The suite's judge grades a description's *content* against a reference output. It has no opinion on
whether the description padded itself with a bullet section restating the diff, or ran long enough
that a reviewer skips it — which is the failure the prose-first prompt exists to prevent. Hence a
per-case ``expect`` block, checked here.
"""

from __future__ import annotations

import re

_KEY_CHANGES = re.compile(r"key\s+changes", re.IGNORECASE)
_BULLET_LINE = re.compile(r"^\s*[-*+]\s+\S", re.MULTILINE)
# A Notes section however the model labels it: bold, markdown heading, or a bare label — but only
# at the start of a line, so prose using the word ("Note that …") is not a section.
_NOTES_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*{1,2})?notes\b\*{0,2}\s*:?\s*$|^\s*(?:\*{1,2})?notes\*{0,2}\s*:", re.IGNORECASE | re.MULTILINE
)
# Deleted, not replaced with a space: `_` is emphasis in markdown but also lives inside
# identifiers, and splitting `TASK_TIMEOUT_SECONDS` into three would make the budget stricter for a
# Python codebase than a JS one.
_INLINE_MARKUP = re.compile(r"[*_`]")
_LINE_MARKUP = re.compile(r"^\s*(?:[#>]+|[-+*])\s*", re.MULTILINE)

# `commit_message`/`branch`/`title` are separate fields DAIV puts elsewhere; a line restating one
# inside the description renders as stray text at the bottom of the MR page. Line-anchored so prose
# ("Reverts the commit that …") is not a leak.
_FIELD_LEAK = re.compile(r"^\s*\**(commit message|commit|branch|title)\**\s*:", re.IGNORECASE | re.MULTILINE)

_SUPPORTED = frozenset({"no_key_changes", "max_words", "has_notes", "no_notes"})


def prose_word_count(description: str) -> int:
    """Words a reviewer actually reads, with markdown punctuation discounted."""
    return len(_INLINE_MARKUP.sub("", _LINE_MARKUP.sub(" ", description)).split())


def shape_violations(description: str, expect: dict) -> list[str]:
    """Every way ``description`` breaks ``expect``, so one run reports all of them."""
    if unknown := set(expect) - _SUPPORTED:
        raise ValueError(f"Unknown shape expectation(s): {', '.join(sorted(unknown))}")

    violations: list[str] = []

    if expect.get("no_key_changes"):
        if _KEY_CHANGES.search(description):
            violations.append("has a 'Key Changes' heading on a change with one concern")
        elif _BULLET_LINE.search(description):
            violations.append("has a bullet list on a change with one concern")

    if (budget := expect.get("max_words")) is not None and (count := prose_word_count(description)) > budget:
        violations.append(f"runs to {count} words, over the {budget}-word budget")

    if expect.get("has_notes") and not _NOTES_HEADING.search(description):
        violations.append("omits the 'Notes' section for a run that reported a caveat")

    if expect.get("no_notes") and _NOTES_HEADING.search(description):
        violations.append("invents a 'Notes' section for a run that reported no caveat")

    if match := _FIELD_LEAK.search(description):
        violations.append(f"restates a sibling field in the description ({match.group(1).strip()})")

    return violations
