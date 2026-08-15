"""Repository-wide guard on how the stylesheet's animations fill.

Lives at the top of tests/unit_tests/ rather than mirroring an app package: there is one
stylesheet and the rule below holds for every rule in it.

An animation that fills forwards keeps its properties under animation control after it
ends, and `transform` then resolves to a matrix — an *identity* matrix for a keyframe set
with only a `from` step, but a matrix either way. Identity is still a transform, so the
element stays a containing block for `position: fixed` descendants, which pinned every
picker bottom sheet to the bottom of its animated card instead of the bottom of the
viewport. Animations that move a transform therefore fill `backwards`: the delay is still
covered, and the transform is released the moment the animation ends.
"""

from __future__ import annotations

import re

from tests.unit_tests.test_template_comments import DAIV_DIR

INPUT_CSS = DAIV_DIR / "static_src" / "css" / "input.css"

# Keyframe blocks nest exactly one level (the steps), so the inner alternation is enough.
KEYFRAMES = re.compile(r"@keyframes\s+([\w-]+)\s*\{((?:[^{}]|\{[^{}]*\})*)\}")
ANIMATION = re.compile(r"animation:\s*([^;]+);")

# The individual transform properties establish a containing block on the same terms.
TRANSFORM = re.compile(r"\b(transform|translate|rotate|scale|perspective)\s*:")

FORWARD_FILL = {"both", "forwards"}


def test_transform_animations_never_fill_forwards():
    source = INPUT_CSS.read_text(encoding="utf-8")
    moves_transform = {name for name, steps in KEYFRAMES.findall(source) if TRANSFORM.search(steps)}

    assert moves_transform, "expected the stylesheet to still carry transform keyframes"

    offenders = [
        shorthand.strip()
        for shorthand in ANIMATION.findall(source)
        if (parts := set(shorthand.split())) & moves_transform and parts & FORWARD_FILL
    ]

    assert not offenders, (
        "A forward fill leaves a transform behind, which makes the element a containing "
        "block for `position: fixed` children. Use `backwards`:\n" + "\n".join(offenders)
    )
