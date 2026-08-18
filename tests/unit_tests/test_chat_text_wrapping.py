"""Repository-wide guard on how chat text wraps a run with no break opportunity.

Lives at the top of tests/unit_tests/ beside the other stylesheet guards: there is one
stylesheet, and a rule anywhere in it can win over the one below.

`overflow-wrap: break-word` breaks a long word once the box is already narrow, but by spec
it leaves the box's *min-content* width at the width of that word. Chat text is sized
intrinsically — the user bubble's inner text is a flex item under `align-items: flex-start`
— so the box grows to fit a pasted URL and the text paints outside the bubble, clipped
rather than scrollable because `<main>` sets `overflow-x: hidden`. Only `overflow-wrap:
anywhere` feeds back into intrinsic sizing, which is why it is the value `.chat-text`
declares and why a more specific rule must not narrow it back to `break-word`.
"""

from __future__ import annotations

import re

from tests.unit_tests.test_template_comments import DAIV_DIR

INPUT_CSS = DAIV_DIR / "static_src" / "css" / "input.css"

# Flat rules only. Nothing that styles chat text nests, and the enclosing `@layer`/`@media`
# preludes can never match, since a selector can't span a brace.
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
WRAP = re.compile(r"\boverflow-wrap:\s*([\w-]+)")
# Stripped first: a comment sits between two rules, so it would otherwise be read as part of
# the selector that follows it.
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# Table headers are the one place a whole word is worth keeping: they are short enough that
# holding them together can't push the table past its container.
WHOLE_WORD_ALLOWED = ("th",)


def _chat_text_wrapping() -> list[tuple[str, str]]:
    """`(selector, overflow-wrap value)` for every rule that wraps a `.chat-text` element."""
    pairs = []
    source = COMMENT.sub(" ", INPUT_CSS.read_text(encoding="utf-8"))
    for selector, body in RULE.findall(source):
        if ".chat-text" in selector and (wrap := WRAP.search(body)):
            pairs.append((" ".join(selector.split()), wrap.group(1)))
    return pairs


def test_chat_text_breaks_a_run_with_no_break_opportunity():
    assert (".chat-text", "anywhere") in _chat_text_wrapping(), (
        "`.chat-text` must declare `overflow-wrap: anywhere` — a pasted URL has no break "
        "opportunity, and every narrower value leaves the box sized to the whole run."
    )


def test_no_rule_narrows_chat_text_wrapping_back():
    offenders = [
        f"{selector} {{ overflow-wrap: {value} }}"
        for selector, value in _chat_text_wrapping()
        if value != "anywhere" and selector.split()[-1] not in WHOLE_WORD_ALLOWED
    ]

    assert not offenders, (
        "These rules win over `.chat-text` and stop it shrinking to its container, so a "
        "pasted URL paints outside the bubble and `<main>` clips it. Use `anywhere`:\n" + "\n".join(offenders)
    )
