"""Behavioral tests for the transcript's Artifacts card in ``chat-stream.js`` and ``tool-renderers.js``, under node."""

from __future__ import annotations

import json

from tests.unit_tests.chat.chat_stream_driver import CHAT_STREAM_JS, run_chat_stream
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

TOOL_RENDERERS_JS = CHAT_STREAM_JS.parent / "tool-renderers.js"

BODY = """
vm.runInContext(readFileSync(payload.renderers, "utf8"), sandbox);
const chat = registry.chat({ endpoint: "/api/chat" });
chat.$watch = () => {};
chat.$nextTick = () => {};
chat.turns = payload.turns;
chat.init();
const rows = (turn) => chat.visibleSegments(turn)
  .filter((s) => s.type === "artifact_group")
  .map((group) => group.items.map((it) => [
    it.state, it.title, it.kindLabel ?? "", it.sizeLabel ?? "", it.url ?? "",
    it.updated ? "Updated" : it.updating ? "Updating…" : "",
  ]));
process.stdout.write(JSON.stringify(chat.turns.map(rows)));
chat.destroy();
"""


def _publish(call_id: str, *, artifact_id: str = "", result: dict | None = None, path: str = "/workspace/tmp/a.md"):
    args = {"path": path, **({"artifact_id": artifact_id} if artifact_id else {})}
    return {
        "type": "tool_call",
        "id": call_id,
        "name": "publish_artifact",
        "args": json.dumps(args),
        "result": None if result is None else json.dumps(result),
        "status": "done" if result is not None else "running",
    }


def _result(status: str, artifact_id: str, title: str, kind: str, size: int) -> dict:
    url = f"/artifacts/{artifact_id}/"
    return {"status": status, "id": artifact_id, "title": title, "kind": kind, "size": size, "url": url}


def _rows(*assistant_turns: list[dict]) -> list:
    turns = []
    for i, segments in enumerate(assistant_turns):
        turns.append({"id": f"u-{i}", "role": "user", "segments": [{"type": "text", "content": "go"}]})
        turns.append({"id": f"a-{i}", "role": "assistant", "segments": segments})
    payload = {"turns": turns, "renderers": str(TOOL_RENDERERS_JS)}
    return run_chat_stream(BODY, payload, extra_globals="surfaceGroup: { join: () => () => {} },")[1::2]


def test_an_earlier_row_shows_what_a_later_revision_published():
    first, second = _rows(
        [_publish("t1", result=_result("published", "A", "Audit", "markdown", 2048))],
        [_publish("t2", artifact_id="A", result=_result("updated", "A", "Audit v2", "html", 10))],
    )

    assert first == [[["published", "Audit v2", "HTML", "10 B", "/artifacts/A/", "Updated"]]]
    assert second == first


def test_a_publish_and_its_revision_in_one_card_share_a_row():
    (turn,) = _rows([
        _publish("t1", result=_result("published", "A", "Audit", "markdown", 2048)),
        _publish("t2", result=_result("published", "B", "Data", "text", 3)),
        _publish("t3", artifact_id="A", result=_result("updated", "A", "Audit v2", "html", 10)),
    ])

    assert turn == [
        [
            ["published", "Audit v2", "HTML", "10 B", "/artifacts/A/", "Updated"],
            ["published", "Data", "Text", "3 B", "/artifacts/B/", ""],
        ]
    ]


def test_a_revision_in_flight_keeps_its_own_row():
    (turn,) = _rows([
        _publish("t1", result=_result("published", "A", "Audit", "markdown", 2048)),
        _publish("t2", artifact_id="A", path="/workspace/tmp/audit.md"),
    ])

    assert turn == [
        [
            ["published", "Audit", "Markdown", "2.0 KB", "/artifacts/A/", ""],
            ["running", "/workspace/tmp/audit.md", "", "", "", "Updating…"],
        ]
    ]
