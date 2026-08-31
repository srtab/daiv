"""Contrast guard for the floating surfaces — sheets, popovers, menus.

Lives at the top of ``tests/unit_tests/`` rather than mirroring an app package, for the
same reason ``test_css_animations`` and ``test_picker_popovers`` do: the surfaces are one
family shared across apps (``core/_sheet_head.html`` renders into every one of them), so a
guard scoped to a single app would leave its siblings unwatched.

These surfaces are darker than the page they float over, so a colour that reads fine in the
body lands below AA on them — which is how a completed todo row ended up at 3.6:1 beside
file paths at 12.8:1. Scope is by surface *membership*: every BEM family that renders
inside one, not the selectors that happen to spell a surface's name.
"""

from __future__ import annotations

import re

from tests.unit_tests.test_picker_popovers import INPUT_CSS
from tests.unit_tests.test_template_comments import DAIV_DIR

DIFF_CSS = DAIV_DIR / "chat" / "static" / "chat" / "css" / "diff.css"

CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
# `(?<!-)` keeps `border-color` and friends out: non-text has no 4.5:1 bar to clear.
TEXT_COLOR = re.compile(r"(?<!-)color:\s*(#[0-9a-fA-F]{3,6})\b")
# A surface paints itself with a hex, an arbitrary utility, or a `--color-*` token; the
# token form is resolved through `THEME_TOKEN` so the guard measures the colour that ships.
SURFACE_BACKGROUND = re.compile(
    r"background:\s*(#[0-9a-fA-F]{6})|bg-\[(#[0-9a-fA-F]{6})\]"
    r"|background:\s*var\((--color-[\w-]+)\)|bg-(?!\[)([\w-]+)"
)
THEME_TOKEN = re.compile(r"(--color-[\w-]+):\s*(#[0-9a-fA-F]{3,6}|var\(--color-[\w-]+\))\s*;")

# The sheet/popover half of the `surface-rise` roster in input.css — the surfaces that open
# over the page rather than sitting in it. `.card__menu-panel` is on the roster too but is a
# dropdown on its own darker background, with no shared rows to measure.
SURFACES = frozenset({".composer-sheet", ".composer-autocomplete", ".picker-popover", ".filter-menu"})

# BEM families rendering inside those surfaces, including the ones whose names say nothing
# about where they mount: `.sheet-row` is the options sheet *and* the env picker, `.chat-todo`
# is the progress sheet.
FAMILIES = re.compile(
    r"\.(composer-sheet|composer-autocomplete|picker-popover|env-popover|sheet-row|sheet-disclosure|filter-menu|chat-todo)"
)

# Decorative punctuation between two values, carrying no information of its own (WCAG 1.4.3
# applies to text that conveys something). Exempt by name so it stays a decision, not a gap.
DECORATIVE = frozenset({".sheet-disclosure__sep"})

AA_NORMAL_TEXT = 4.5


def _relative_luminance(colour: str) -> float:
    digits = colour.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    channels = []
    for offset in (0, 2, 4):
        c = int(digits[offset : offset + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_relative_luminance(foreground) + 0.05, _relative_luminance(background) + 0.05))
    return darker / lighter


def theme_colours(path=None) -> dict[str, str]:
    """Every `--color-*` in `input.css`'s `@theme`, dereferenced to a hex.

    A token may point at another token (`--color-focus: var(--color-accent-bright)`), so
    the values are followed until they land on a literal.
    """
    source = (path or INPUT_CSS).read_text(encoding="utf-8")
    declared = dict(THEME_TOKEN.findall(source))
    resolved = {}
    for name, value in declared.items():
        seen = set()
        while value.startswith("var(") and name not in seen:
            seen.add(name)
            value = declared[value[len("var(") : -1]]
        resolved[name] = value
    return resolved


def surface_background(body: str, tokens: dict[str, str]) -> str | None:
    """The hex a surface's rule paints, whether spelled literally or as a token."""
    for hex_value, applied_hex, token, utility in SURFACE_BACKGROUND.findall(body):
        if hex_value or applied_hex:
            return hex_value or applied_hex
        name = token or f"--color-{utility}"
        if name in tokens:
            return tokens[name]
    return None


def iter_rules(*paths):
    """Selector/body pairs, comments stripped — an unstripped rule carries the preceding
    comment into its selector, which would let prose decide what gets measured."""
    for path in paths:
        source = CSS_COMMENT.sub("", path.read_text(encoding="utf-8"))
        for selector, body in CSS_RULE.findall(source):
            yield " ".join(selector.split()), body


def test_the_floating_surfaces_share_one_background():
    """The guard below measures one background because these surfaces declare one. Adding a
    surface, or re-toning an existing one, has to come back through here."""
    tokens = theme_colours()
    backgrounds = {}
    for selector, body in iter_rules(INPUT_CSS):
        block = selector.split()[0].split(",")[0].split(":")[0]
        if block not in SURFACES:
            continue
        if (background := surface_background(body, tokens)) is not None:
            backgrounds.setdefault(block, background)

    assert set(backgrounds) == SURFACES, f"unenrolled or renamed surface: {set(backgrounds) ^ SURFACES}"
    assert len(set(backgrounds.values())) == 1, f"surfaces no longer agree on a background: {backgrounds}"


def test_surface_text_clears_aa_on_the_surface_background():
    """Every colour that renders on a floating surface, not just the ones whose selector
    spells the surface's name — `.sheet-row__meta` mounts in the options sheet and the env
    picker without either word appearing in it."""
    tokens = theme_colours()
    background = next(
        background
        for selector, body in iter_rules(INPUT_CSS)
        if selector.split()[0].split(",")[0] == ".composer-sheet"
        if (background := surface_background(body, tokens)) is not None
    )

    measured = [
        (selector, colour)
        for selector, body in iter_rules(INPUT_CSS, DIFF_CSS)
        if FAMILIES.search(selector) and selector not in DECORATIVE
        for colour in TEXT_COLOR.findall(body)
    ]

    assert len(measured) >= 20, f"the guard stopped seeing the surfaces: {len(measured)} colours"

    failing = {
        f"{selector} ({colour})": round(ratio, 2)
        for selector, colour in measured
        if (ratio := _contrast(colour, background)) < AA_NORMAL_TEXT
    }
    assert not failing, f"below AA on {background}: {failing}"
