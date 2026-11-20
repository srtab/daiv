"""Behavioral tests for the list page's live status stream (``sessions/js/session-stream.js``), run under node."""

from __future__ import annotations

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

pytestmark = requires_node

STREAM_JS = DAIV_DIR / "sessions" / "static" / "sessions" / "js" / "session-stream.js"

HARNESS = """
import vm from "node:vm";
import fs from "node:fs";

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const listeners = {};
const rows = {};
const elements = {};
const sources = [];
const changed = [];
const sandbox = {
  console,
  JSON,
  EventSource: class {
    static CLOSED = 2;
    constructor(url) { this.url = url; sources.push(this); }
    close() {}
  },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  dispatchEvent: (event) => changed.push(event.detail),
  document: {
    addEventListener: (name, fn) => { listeners[name] = fn; },
    getElementById: (id) => elements[id] ?? null,
    querySelector: (selector) => rows[selector.match(/data-run-id="([^"]+)"/)?.[1]] ?? null,
  },
};
sandbox.window = sandbox;
const registry = {};
sandbox.Alpine = { data: (name, factory) => { registry[name] = factory; } };
vm.createContext(sandbox);
vm.runInContext(payload.stream, sandbox);
listeners["alpine:init"]();
const frame = (data) => sources.at(-1).onmessage({ data: JSON.stringify(data) });
const stream = registry.sessionStream("/stream/", "1,2");
stream.init();
const result = (() => { BODY })();
process.stdout.write(JSON.stringify({ result: result ?? null, changed }));
"""


def _run(body: str) -> dict:
    return run_node(HARNESS.replace("BODY", body), {"stream": STREAM_JS.read_text(encoding="utf-8")})


def test_only_a_change_to_a_shown_status_asks_for_a_refresh():
    out = _run("""
        rows["1"] = { dataset: { runStatus: "RUNNING" } };
        frame({ id: 1, status: "RUNNING" });
        frame({ id: 2, status: "READY" });
        frame({ id: 2, status: "RUNNING" });
        frame({ id: 1, status: "SUCCESSFUL" });
    """)

    assert out["changed"] == [{"id": 1, "status": "SUCCESSFUL"}]


def test_a_swap_drops_the_statuses_the_old_rows_showed():
    out = _run("""
        rows["1"] = { dataset: { runStatus: "RUNNING" } };
        frame({ id: 1, status: "WAITING_INPUT" });
        elements["session-in-flight"] = { dataset: { ids: "1" } };
        rows["1"].dataset.runStatus = "RUNNING";
        stream.reconnect();
        frame({ id: 1, status: "RUNNING" });
        return stream.rowClass("1", "RUNNING");
    """)

    assert out["changed"] == [{"id": 1, "status": "WAITING_INPUT"}]
    assert out["result"] == {"session-row--failed": False}
