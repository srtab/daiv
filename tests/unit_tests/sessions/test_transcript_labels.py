from __future__ import annotations

import re

from django.contrib.staticfiles import finders

from sessions.transcript_labels import TRANSCRIPT_ICONS, transcript_icons, transcript_labels

from tests.unit_tests.chat.transcript_driver import run_transcript
from tests.unit_tests.jsdriver import requires_node
from tests.unit_tests.test_template_comments import DAIV_DIR

pytestmark = requires_node

CHAT_JS = DAIV_DIR / "chat" / "static" / "chat" / "js"
TRANSCRIPT_JS = ("tool-renderers.js", "transcript-blocks.js", "chat-stream.js", "transcript-labels.js")
ICON_LITERAL = re.compile(r"""\bicon:\s*["']([\w-]+)["']|iconHTML\(\s*["']([\w-]+)["']""")

SHAPES = """Object.fromEntries(
  Object.entries(TranscriptLabels.defaults).map(([key, value]) => [key, Array.isArray(value) ? "pair" : "text"]),
)"""


def _browser_shapes() -> dict[str, str]:
    return run_transcript(SHAPES, {}, blocks=False)


def test_server_labels_cover_exactly_the_browser_defaults():
    assert set(transcript_labels()) == set(_browser_shapes())


def test_plural_labels_are_pairs_on_both_sides():
    shapes = _browser_shapes()
    for key, value in transcript_labels().items():
        assert isinstance(value, list) == (shapes[key] == "pair"), key


def test_every_icon_the_transcript_js_names_ships_rendered():
    sources = [(CHAT_JS / name).read_text(encoding="utf-8") for name in TRANSCRIPT_JS]
    named = {a or b for source in sources for a, b in ICON_LITERAL.findall(source)}
    assert {"document-text", "puzzle-piece", "light-bulb"} <= named
    assert named <= set(transcript_icons())
    assert [name for name in TRANSCRIPT_ICONS if not finders.find(f"core/img/icons/{name}.svg")] == []
