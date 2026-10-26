from __future__ import annotations

from tests.unit_tests.chat.chat_stream_driver import run_chat_stream
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

BODY = """
const chat = registry.chat({ endpoint: "/api/chat" });
chat.slashCatalog = payload.rows;
const out = {};
for (const draft of payload.drafts) {
  chat.draftMessage = draft;
  out[draft] = chat.slashMatches.map((row) => row.name);
}
process.stdout.write(JSON.stringify(out));
"""

ROWS = [
    {"name": "help", "description": "", "kind": "command"},
    {"name": "superpowers:brainstorming", "description": "", "kind": "skill"},
    {"name": "superpowers:debugging", "description": "", "kind": "skill"},
    {"name": "other:brainstorming", "description": "", "kind": "skill"},
]


def test_a_qualified_plugin_prefix_keeps_the_menu_open_and_narrows():
    out = run_chat_stream(BODY, {"rows": ROWS, "drafts": ["/superpowers:", "/superpowers:bra"]})

    assert out["/superpowers:"] == ["superpowers:brainstorming", "superpowers:debugging"]
    assert out["/superpowers:bra"] == ["superpowers:brainstorming"]
