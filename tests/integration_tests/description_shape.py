"""Deterministic checks on the shape of a generated PR description.

The suite's judge grades a description's *content* against a reference output. It has no opinion on
whether the description padded itself with a bullet section restating the diff, or ran long enough
that a reviewer skips it — which is the failure the prose-first prompt exists to prevent. Hence a
per-case ``expect`` block, checked here.
"""

from __future__ import annotations

import re

_KEY_CHANGES = re.compile(r"^\s*\**\s*#*\s*key\s+changes", re.IGNORECASE | re.MULTILINE)
# Ordered markers too: dropping the heading and numbering the same restated hunks is the same
# padding, and models reach for `1.` as readily as `-`.
_BULLET_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE)
# A Notes section however the model labels it: bold, markdown heading, or a bare label — but only
# at the start of a line, so prose using the word ("Note that …") is not a section.
_NOTES_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*{1,2})?notes\b\*{0,2}\s*:?\s*$|^\s*(?:\*{1,2})?notes\*{0,2}\s*:", re.IGNORECASE | re.MULTILINE
)
# Deleted, not spaced: `_` is markdown emphasis but also lives inside identifiers, and splitting
# `TASK_TIMEOUT_SECONDS` into three would make the budget stricter for Python than for JS.
_INLINE_MARKUP = re.compile(r"[*_`]")
_LINE_MARKUP = re.compile(r"^\s*(?:[#>]+|[-+*])\s*", re.MULTILINE)

# These are separate fields rendered elsewhere, so a line restating one shows up as stray text at
# the bottom of the MR. Line-anchored, so prose ("Reverts the commit that …") is not a leak.
_FIELD_LEAK = re.compile(r"^\s*\**(commit message|commit|branch|title)\**\s*:", re.IGNORECASE | re.MULTILINE)

# Both section checks are line-anchored, so a label inside a list item would slip past them; the
# bullet is the form a model reaches for when the prompt asked for bullets two paragraphs earlier.
_LIST_PREFIX = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+", re.MULTILINE)

_FLAGS = frozenset({"no_key_changes", "has_notes", "no_notes"})
_SUPPORTED = _FLAGS | {"max_words"}


def prose_word_count(description: str) -> int:
    """Words a reviewer actually reads, with markdown punctuation discounted."""
    return len(_INLINE_MARKUP.sub("", _LINE_MARKUP.sub(" ", description)).split())


def validate_expectation(expect: dict) -> None:
    """Reject a malformed ``expect`` block.

    Called from ``load_cases`` so a typo in ``cases.jsonl`` fails at collection rather than after
    one paid model call per case per model.
    """
    if unknown := set(expect) - _SUPPORTED:
        raise ValueError(f"Unknown shape expectation(s): {', '.join(sorted(unknown))}")
    if expect.get("has_notes") and expect.get("no_notes"):
        raise ValueError("has_notes and no_notes are contradictory; a description cannot satisfy both")
    for flag in _FLAGS:
        if flag in expect and not isinstance(expect[flag], bool):
            raise ValueError(f"{flag} must be a bool, got {type(expect[flag]).__name__}")
    if (budget := expect.get("max_words")) is not None and (isinstance(budget, bool) or not isinstance(budget, int)):
        raise ValueError(f"max_words must be an int, got {type(budget).__name__}")


def shape_violations(description: str, expect: dict) -> list[str]:
    """Every way ``description`` breaks ``expect``, so one run reports all of them."""
    validate_expectation(expect)

    violations: list[str] = []
    unlisted = _LIST_PREFIX.sub("", description)

    if expect.get("no_key_changes"):
        if _KEY_CHANGES.search(description):
            violations.append("has a 'Key Changes' heading on a change with one concern")
        elif _BULLET_LINE.search(description):
            violations.append("has a bullet list on a change with one concern")

    if (budget := expect.get("max_words")) is not None and (count := prose_word_count(description)) > budget:
        violations.append(f"runs to {count} words, over the {budget}-word budget")

    if expect.get("has_notes") and not _NOTES_HEADING.search(unlisted):
        violations.append("omits the 'Notes' section for a run that reported a caveat")

    if expect.get("no_notes") and _NOTES_HEADING.search(unlisted):
        violations.append("invents a 'Notes' section for a run that reported no caveat")

    if match := _FIELD_LEAK.search(unlisted):
        violations.append(f"restates a sibling field in the description ({match.group(1).strip()})")

    return violations
