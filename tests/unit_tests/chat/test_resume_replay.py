"""Behavioral tests for the rejoin path in ``chat-stream.js``.

The chat page rebuilds a running turn from two sources at once — the server-rendered
checkpoint and a full replay of the run's relay stream — and the reconciliation between
them lives entirely in JS. These tests drive the real module under node (stubbing only
the DOM and ``EventSource``) because the failure they guard is invisible to a
source-string assertion: a replayed event that renders a *second* copy of something the
checkpoint already painted.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage

from chat.turns import build_turns
from tests.unit_tests.chat.test_composer_template import CHAT_STREAM_JS
from tests.unit_tests.jsdriver import requires_node, run_node

pytestmark = requires_node

# Chronological relay frames for a run whose first model call is already checkpointed
# (message ``msg-1``, tool ``tc-1``) and whose second is still in flight. ``timestamp`` is
# the server stamp ``chat.api.runner._publish`` adds: thought "a" spans 1100→4000ms,
# thought "b" 9100→16100ms.
REPLAY_FRAMES = [
    {"type": "RUN_STARTED", "timestamp": 1000},
    {"type": "REASONING_START", "messageId": "reason-a", "timestamp": 1100},
    {"type": "REASONING_MESSAGE_CONTENT", "messageId": "reason-a", "delta": "thought", "timestamp": 1200},
    {"type": "REASONING_END", "messageId": "reason-a", "timestamp": 4000},
    {"type": "TEXT_MESSAGE_START", "messageId": "msg-1", "timestamp": 4100},
    {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-1", "delta": "Checking the deps.", "timestamp": 4200},
    {"type": "TEXT_MESSAGE_END", "messageId": "msg-1", "timestamp": 4300},
    {"type": "TOOL_CALL_START", "toolCallId": "tc-1", "toolCallName": "bash", "timestamp": 4400},
    {"type": "TOOL_CALL_ARGS", "toolCallId": "tc-1", "delta": "{}", "timestamp": 4500},
    {"type": "TOOL_CALL_END", "toolCallId": "tc-1", "timestamp": 4600},
    {"type": "TOOL_CALL_RESULT", "toolCallId": "tc-1", "content": "ok", "timestamp": 9000},
    # In-flight model call: not in the checkpoint yet, so nothing dedupes it.
    {"type": "REASONING_START", "messageId": "reason-b", "timestamp": 9100},
    {"type": "REASONING_MESSAGE_CONTENT", "messageId": "reason-b", "delta": "in-flight thought", "timestamp": 9200},
    {"type": "REASONING_END", "messageId": "reason-b", "timestamp": 16100},
    {"type": "TEXT_MESSAGE_START", "messageId": "msg-2", "timestamp": 16200},
    {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-2", "delta": "The lockfile is stale.", "timestamp": 16300},
]

# Everything up to (and including) the first delta of the in-flight thought: the page
# rejoined while the model was still thinking.
FRAMES_MID_THOUGHT = REPLAY_FRAMES[:13]

# What the server already rendered when the page loaded, through the same helper the view
# uses — a hand-written literal would keep passing after ``build_turns`` changed shape,
# which is the exact reconciliation these tests exist to pin.
HYDRATED_TURNS = build_turns([
    AIMessage(
        id="msg-1",
        content=[
            {"type": "thinking", "thinking": "thought"},
            {"type": "text", "text": "Checking the deps."},
            {"type": "tool_use", "id": "tc-1", "name": "bash", "input": {}},
        ],
        tool_calls=[{"id": "tc-1", "name": "bash", "args": {}}],
    ),
    ToolMessage(content="ok", tool_call_id="tc-1"),
])

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, frames, turns } = JSON.parse(readFileSync(0, "utf8"));
const registry = {};
let opened = null;

class FakeEventSource {
  static CLOSED = 2;
  constructor() { this.readyState = 1; this.listeners = {}; opened = this; }
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
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const chat = registry.chat({ endpoint: "/api/chat", streamEndpoint: "/api/chat/stream" });
chat.turns = turns;
chat.thread = { thread_id: "t1", repo_id: "r", ref: "main" };
chat.$nextTick = (cb) => cb();
chat.scrollToBottom = () => {};

const settled = chat._resumeRun("run-1");
for (const evt of frames) opened.onmessage({ data: JSON.stringify(evt) });
opened.listeners.end({ data: JSON.stringify({ reason: "finished" }) });
await settled;

const turn = chat.turns.filter((t) => t.role === "assistant").pop();
process.stdout.write(JSON.stringify(turn.segments.map((s) => ({ ...s, label: chat.thinkingLabel(s) }))));
"""


def _rejoin(frames) -> list[dict]:
    """Replay ``frames`` into a freshly rejoined turn; return its rendered segments."""
    return run_node(HARNESS, {"src": str(CHAT_STREAM_JS), "frames": frames, "turns": HYDRATED_TURNS})


def test_rejoin_drops_thinking_the_checkpoint_already_rendered():
    """Reasoning is the one event family the replay dedup cannot match by id: its
    ``messageId`` is per-thought (the provider's id, else a fresh uuid) and appears in no
    checkpoint. Left unhandled, every thought of the run re-renders in the rejoin turn —
    and back-to-back, since the text and tool events between them *are* deduped.
    """
    segments = _rejoin(REPLAY_FRAMES)

    assert [(s["type"], s["content"]) for s in segments] == [
        ("thinking", "in-flight thought"),
        ("text", "The lockfile is stale."),
    ]


def test_a_thought_still_open_on_rejoin_survives():
    """The prefix drop is positional, so the boundary matters: rejoining *while* the model
    thinks must keep that thought — it is the only thing the turn has to show. Settling it
    reuses the last frame's clock rather than mixing the client's into the elapsed.
    """
    segments = _rejoin(FRAMES_MID_THOUGHT)

    assert [(s["type"], s["content"]) for s in segments] == [("thinking", "in-flight thought")]
    assert segments[0]["endedAt"] == 9200


def test_replayed_thoughts_are_timed_by_the_server_not_the_replay():
    """A whole run's frames arrive in one burst on rejoin, so a client clock reports every
    thought as the 1s floor. The relay's stamped emit time is what makes elapsed real.
    """
    segments = _rejoin(REPLAY_FRAMES)

    thought = next(s for s in segments if s["type"] == "thinking")
    assert thought["endedAt"] - thought["startedAt"] == 7000
    assert thought["label"] == "Thought for 7s"
