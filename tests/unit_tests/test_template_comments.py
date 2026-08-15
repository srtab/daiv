"""Repository-wide guard against template comment markup reaching the page.

Lives at the top of tests/unit_tests/ rather than mirroring an app package: it covers
every template the project ships, not a daiv module.

Django lexes `{#` comments with `{#.*?#}` and no `re.DOTALL`, so a `{# ... #}` broken
across two lines is never a comment token — it stays text and renders verbatim to the
user. Nothing else catches it: the template compiles, the page returns 200, and the
note shows up in the UI. Multi-line notes need `{% comment %}`/`{% endcomment %}`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from django.template.base import Lexer, TokenType

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
DAIV_DIR = REPO_ROOT / "daiv"

COMMENT_TAGS = {"comment": 1, "endcomment": -1}


def iter_template_files() -> Iterator[Path]:
    """Every file under a `templates/` directory in the project source tree."""
    for templates_dir in sorted(DAIV_DIR.rglob("templates")):
        if templates_dir.is_dir():
            yield from sorted(p for p in templates_dir.rglob("*") if p.is_file())


def leaked_comments(source: str) -> list[tuple[int, str]]:
    """Line numbers and text of comment markup that survives lexing into rendered output.

    Text inside a `{% comment %}` block is discarded at compile time, so `{#` written
    there is not a leak; depth tracking keeps those out of the results.
    """
    leaks = []
    depth = 0
    for token in Lexer(source).tokenize():
        if token.token_type is TokenType.BLOCK:
            tag, _, _ = token.contents.partition(" ")
            depth = max(0, depth + COMMENT_TAGS.get(tag, 0))
        elif token.token_type is TokenType.TEXT and depth == 0:
            for offset, line in enumerate(token.contents.splitlines()):
                if "{#" in line:
                    leaks.append((token.lineno + offset, line.strip()))
    return leaks


def test_leaked_comments_detects_only_the_unrenderable_form():
    """Pin the detector: single-line and block comments are consumed, a split one is not."""
    source = "\n".join([
        "{# single line #}",
        "{% comment %}",
        "a block note mentioning {# a comment #} in passing",
        "{% endcomment %}",
        "{# split across",
        "   two lines #}",
        "<p>body</p>",
    ])

    assert leaked_comments(source) == [(5, "{# split across")]


def test_no_template_comment_markup_reaches_the_user():
    """No template renders `{#` markup as visible text."""
    failures: list[str] = []
    scanned = 0

    for path in iter_template_files():
        rel = path.relative_to(REPO_ROOT)
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"{rel}: not valid UTF-8, so it cannot be scanned for comment markup")
            continue
        scanned += 1
        failures.extend(f"{rel}:{lineno}: {text}" for lineno, text in leaked_comments(source))

    # Real breakage is reported before the floor, which would otherwise mask it.
    assert not failures, (
        "Template comment markup that renders as visible text — use {% comment %} for multi-line notes:\n"
        + "\n".join(failures)
    )
    assert scanned > 100, f"only {scanned} templates scanned — DAIV_DIR or the templates layout drifted"
