from __future__ import annotations

import re

from django.contrib.staticfiles import finders

from sessions.transcript_labels import transcript_icon_urls, transcript_labels

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

pytestmark = requires_node

CHAT_JS = DAIV_DIR / "chat" / "static" / "chat" / "js"
TRANSCRIPT_LABELS_JS = CHAT_JS / "transcript-labels.js"
TRANSCRIPT_JS = ("tool-renderers.js", "transcript-blocks.js", "chat-stream.js", "transcript-labels.js")
ICON_LITERAL = re.compile(r"""\bicon:\s*["']([\w-]+)["']|iconHTML\(\s*["']([\w-]+)["']""")

_BOOT = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const payload = JSON.parse(readFileSync(0, "utf8"));
const sandbox = { document: { getElementById: () => null } };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(readFileSync(payload.src, "utf8"), sandbox);
const DEFAULTS = vm.runInContext("TranscriptLabels.defaults", sandbox);
"""

KEYS_SCRIPT = _BOOT + "process.stdout.write(JSON.stringify(Object.keys(DEFAULTS)));"
SHAPES_SCRIPT = (
    _BOOT
    + """
const shapes = {};
for (const [key, value] of Object.entries(DEFAULTS)) shapes[key] = Array.isArray(value) ? "pair" : "text";
process.stdout.write(JSON.stringify(shapes));
"""
)


def test_server_labels_cover_exactly_the_browser_defaults():
    js_keys = set(run_node(KEYS_SCRIPT, {"src": str(TRANSCRIPT_LABELS_JS)}))
    assert set(transcript_labels()) == js_keys


def test_plural_labels_are_pairs_on_both_sides():
    shapes = run_node(SHAPES_SCRIPT, {"src": str(TRANSCRIPT_LABELS_JS)})
    for key, value in transcript_labels().items():
        assert isinstance(value, list) == (shapes[key] == "pair"), key


def test_every_icon_the_transcript_js_names_ships_with_its_url():
    sources = [(CHAT_JS / name).read_text(encoding="utf-8") for name in TRANSCRIPT_JS]
    named = {a or b for source in sources for a, b in ICON_LITERAL.findall(source)}
    assert {"document-text", "puzzle-piece", "light-bulb"} <= named
    assert named <= set(transcript_icon_urls())
    assert [name for name in transcript_icon_urls() if not finders.find(f"core/img/icons/{name}.svg")] == []
