"""Behavioral tests for the rejoin path in ``chat-stream.js``.

The chat page rebuilds a running turn from two sources at once — the server-rendered
checkpoint and a full replay of the run's relay stream — and the reconciliation between
them lives entirely in JS. These tests drive the real module under node (stubbing only
the DOM and ``EventSource``) because the failure they guard is invisible to a
source-string assertion: a replayed event that renders a *second* copy of something the
checkpoint already painted.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404

import pytest

from tests.unit_tests.chat.test_composer_template import CHAT_STREAM_JS

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to drive chat-stream.js")

# Chronological relay frames for a run whose first model call is already checkpointed
# (message ``msg-1``, tool ``tc-1``) and whose second is still in flight. Frame ids are
# Redis stream ids: reasoning "a" spans 1100→4000ms, reasoning "b" 9100→16100ms.
REPLAY_FRAMES = [
    ("1000-0", {"type": "RUN_STARTED"}),
    ("1100-0", {"type": "REASONING_START", "messageId": "reason-a"}),
    ("1200-0", {"type": "REASONING_MESSAGE_CONTENT", "messageId": "reason-a", "delta": "checkpointed thought"}),
    ("4000-0", {"type": "REASONING_END", "messageId": "reason-a"}),
    ("4100-0", {"type": "TEXT_MESSAGE_START", "messageId": "msg-1"}),
    ("4200-0", {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-1", "delta": "Checking the deps."}),
    ("4300-0", {"type": "TEXT_MESSAGE_END", "messageId": "msg-1"}),
    ("4400-0", {"type": "TOOL_CALL_START", "toolCallId": "tc-1", "toolCallName": "bash"}),
    ("4500-0", {"type": "TOOL_CALL_ARGS", "toolCallId": "tc-1", "delta": "{}"}),
    ("4600-0", {"type": "TOOL_CALL_END", "toolCallId": "tc-1"}),
    ("9000-0", {"type": "TOOL_CALL_RESULT", "toolCallId": "tc-1", "content": "ok"}),
    ("9100-0", {"type": "REASONING_START", "messageId": "reason-b"}),
    ("9200-0", {"type": "REASONING_MESSAGE_CONTENT", "messageId": "reason-b", "delta": "in-flight thought"}),
    ("16100-0", {"type": "REASONING_END", "messageId": "reason-b"}),
    ("16200-0", {"type": "TEXT_MESSAGE_START", "messageId": "msg-2"}),
    ("16300-0", {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-2", "delta": "The lockfile is stale."}),
]

# What the server already rendered from the checkpoint when the page loaded.
HYDRATED_TURNS = [
    {
        "id": "msg-1",
        "role": "assistant",
        "segments": [
            {"type": "thinking", "content": "checkpointed thought"},
            {"type": "text", "content": "Checking the deps."},
            {"type": "tool_call", "id": "tc-1", "name": "bash", "args": "{}", "result": "ok", "status": "done"},
        ],
    }
]

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const [srcPath, framesPath, turnsPath, sendEnd] = process.argv.slice(2);
const registry = {};
let opened = null;

class FakeEventSource {
  static CLOSED = 2;
  constructor() {
    this.readyState = 1;
    this.listeners = {};
    opened = this;
  }
  addEventListener(name, cb) { this.listeners[name] = cb; }
  close() { this.readyState = 2; }
}

const sandbox = {
  console: { log: () => {}, debug: () => {}, warn: () => {}, error: () => {} },
  crypto, setInterval, clearInterval,
  EventSource: FakeEventSource,
  CSS: { supports: () => true },
  document: {
    getElementById: () => null,
    querySelector: () => null,
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); },
  },
  window: { Alpine: { data: (name, factory) => (registry[name] = factory) } },
};
vm.createContext(sandbox);
vm.runInContext(readFileSync(srcPath, "utf8"), sandbox);

const chat = registry.chat({ endpoint: "/api/chat", streamEndpoint: "/api/chat/stream" });
chat.turns = JSON.parse(readFileSync(turnsPath, "utf8"));
chat.thread = { thread_id: "t1", repo_id: "r", ref: "main" };
chat.$nextTick = (cb) => cb();
chat.scrollToBottom = () => {};

const settled = chat._resumeRun("run-1");
for (const [id, evt] of JSON.parse(readFileSync(framesPath, "utf8"))) {
  opened.onmessage({ data: JSON.stringify(evt), lastEventId: id });
}
if (sendEnd === "1") {
  opened.listeners.end({ data: JSON.stringify({ reason: "finished" }) });
  await settled;
} else {
  chat._finishTurn(chat.turns.filter((t) => t.role === "assistant").pop(), "connection_lost");
}

const turn = chat.turns.filter((t) => t.role === "assistant").pop();
process.stdout.write(JSON.stringify(turn.segments.map((s) => ({ ...s, label: chat.thinkingLabel(s) }))));
"""


def _rejoin(tmp_path, frames, *, send_end: bool = True) -> list[dict]:
    """Replay ``frames`` into a freshly rejoined turn; return its rendered segments."""
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    (tmp_path / "frames.json").write_text(json.dumps(frames), encoding="utf-8")
    (tmp_path / "turns.json").write_text(json.dumps(HYDRATED_TURNS), encoding="utf-8")
    proc = subprocess.run(  # noqa: S603
        [
            str(NODE),
            str(harness),
            str(CHAT_STREAM_JS),
            str(tmp_path / "frames.json"),
            str(tmp_path / "turns.json"),
            "1" if send_end else "0",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_rejoin_drops_thinking_the_checkpoint_already_rendered(tmp_path):
    """Reasoning is the one event family the replay dedup cannot match by id: its
    ``messageId`` is per-thought (the provider's id, else a fresh uuid) and appears in no
    checkpoint. Left unhandled, every thought of the run re-renders in the rejoin turn —
    and back-to-back, since the text and tool events between them *are* deduped.
    """
    segments = _rejoin(tmp_path, REPLAY_FRAMES)

    thoughts = [s["content"] for s in segments if s["type"] == "thinking"]
    assert thoughts == ["in-flight thought"]
    assert [s["type"] for s in segments] == ["thinking", "text"]


def test_a_thought_still_open_on_rejoin_survives(tmp_path):
    """The prefix drop is positional, so the boundary matters: rejoining *while* the model
    thinks must keep that thought (it is the only thing the turn has to show).
    """
    segments = _rejoin(tmp_path, REPLAY_FRAMES[:13], send_end=False)

    assert [(s["type"], s["content"]) for s in segments] == [("thinking", "in-flight thought")]


def test_replayed_thoughts_are_timed_by_the_relay_not_the_replay(tmp_path):
    """A replayed START/END pair lands in the same millisecond, so a client clock reports
    every rejoined thought as the 1s floor. Relay entry ids carry the publish time.
    """
    segments = _rejoin(tmp_path, REPLAY_FRAMES)

    thought = next(s for s in segments if s["type"] == "thinking")
    assert thought["endedAt"] - thought["startedAt"] == 7000
    assert thought["label"] == "Thought for 7s"
