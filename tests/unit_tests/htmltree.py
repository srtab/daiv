"""Walk rendered HTML by element, tracking what encloses what.

Two guards need the ancestor chain of a rendered page — which elements a floating surface
sits inside, and which controls the chat composer owns — and neither can use a real DOM: the
dependency tree carries no HTML parser beyond the standard library's.

End tags close back to the nearest open element of that name, the way a browser recovers.
Django templates leave tags implicitly closed (a ``<p>`` ended by its parent's ``</div>``),
and a stack that pops blind drifts from the first of them on, silently shortening the
ancestor chain of everything after it — which reads as a passing guard.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# HTML's void elements: they have no end tag, so pushing them would unbalance the stack.
VOID_TAGS = frozenset({
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
})


class ElementStack(HTMLParser):
    """Calls ``visit`` per start tag, with the element's ancestors in ``stack``."""

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, list[str]]] = []

    def visit(self, tag: str, classes: list[str], attrs: dict[str, str]) -> None:
        """Hook: ``self.stack`` holds this element's ancestors, outermost first."""

    def within(self, predicate: Callable[[str, list[str]], bool]) -> bool:
        """Whether any open ancestor satisfies ``predicate(tag, classes)``."""
        return any(predicate(tag, classes) for tag, classes in self.stack)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        self.visit(tag, classes, attributes)
        if tag not in VOID_TAGS:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        for depth in range(len(self.stack) - 1, -1, -1):
            if self.stack[depth][0] == tag:
                del self.stack[depth:]
                return
