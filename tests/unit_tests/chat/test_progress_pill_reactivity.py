"""Behavioral tests for the composer's progress pill under Alpine's reactivity.

Half a dozen bindings read ``progressPill`` — its own ``x-show``, the tone class, the live
dot, the label, the crowded modifier on the action row, the ``$watch`` that closes an
orphaned sheet. Alpine tracks dependencies *per effect*, so a transcript getter that
answers the second reader of a flush from a cache leaves that binding subscribed to
nothing: it never runs again, and the pill it drives stays as it was until a reload
rebuilds the page. A source-string assertion cannot see that, so these drive the real
module under node against a stand-in for the reactivity it runs on.
"""

from __future__ import annotations

from tests.unit_tests.chat.test_composer_template import CHAT_STREAM_JS
from tests.unit_tests.jsdriver import requires_node, run_node

pytestmark = requires_node

TODOS = [
    {"content": "Define audit sub-areas from repo layout", "status": "completed"},
    {"content": "Launch parallel audit subagents per sub-area", "status": "in_progress"},
]

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, todos } = JSON.parse(readFileSync(0, "utf8"));
const registry = {};

/* A stand-in for Alpine's reactivity, small enough to read: per-effect dependency
   tracking over proxies, effects run synchronously. Synchronous is the point — Alpine's
   initial tree walk runs every binding in one task, and its scheduler flushes the whole
   queued batch inside one microtask, so a value cached "for the rest of the microtask" is
   shared by every reader of the flush and only the first of them is ever asked what it
   read. */
let active = null;
const deps = new WeakMap();
const proxies = new WeakMap();

const track = (target, key) => {
  if (!active) return;
  let keys = deps.get(target);
  if (!keys) deps.set(target, (keys = new Map()));
  let subscribers = keys.get(key);
  if (!subscribers) keys.set(key, (subscribers = new Set()));
  subscribers.add(active);
};

const trigger = (target, key) => {
  for (const run of [...(deps.get(target)?.get(key) ?? [])]) run();
};

const reactive = (value) => {
  if (!value || typeof value !== "object") return value;
  if (proxies.has(value)) return proxies.get(value);
  const proxy = new Proxy(value, {
    get(target, key, receiver) {
      track(target, key);
      return reactive(Reflect.get(target, key, receiver));
    },
    set(target, key, next, receiver) {
      const accepted = Reflect.set(target, key, next, receiver);
      trigger(target, key);
      trigger(target, "length");
      return accepted;
    },
  });
  proxies.set(value, proxy);
  return proxy;
};

const effect = (fn) => {
  const run = () => {
    const previous = active;
    active = run;
    try { fn(); } finally { active = previous; }
  };
  run();
  return run;
};

/* `queueMicrotask` is here so a reintroduced cache fails on the assertions below rather
   than on a missing global — the tests have to describe what the pill did, not crash. */
const sandbox = {
  console: { log: () => {}, debug: () => {}, warn: () => {}, error: () => {} },
  crypto, setInterval, clearInterval, queueMicrotask,
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

const state = reactive(registry.chat({ endpoint: "/api/chat" }));
state.turns = [{ role: "user", segments: [{ type: "text", content: "audit this" }] },
               { role: "assistant", streaming: true, segments: [] }];
state.streaming = true;

// Two bindings reading the same getter in one pass, the way the pill's `x-show` and its
// label do. Each records what it saw, every time it ran.
const seen = { first: [], second: [] };
effect(() => { seen.first.push(state.progressPill?.label ?? null); });
effect(() => { seen.second.push(state.progressPill?.label ?? null); });

// The agent calls write_todos mid-turn: the tool call is appended to the open turn, then
// its args stream in.
const turn = state.turns[state.turns.length - 1];
turn.segments.push({ id: "tc-1", type: "tool_call", name: "write_todos", args: "", status: "running" });
turn.segments[0].args += JSON.stringify({ todos });

process.stdout.write(JSON.stringify(seen));
"""


def _bindings() -> dict[str, list[str | None]]:
    """Replay a mid-turn ``write_todos`` under two bindings; return what each one saw."""
    return run_node(HARNESS, {"src": str(CHAT_STREAM_JS), "todos": TODOS})


def test_every_binding_sees_the_todos_that_land_mid_turn():
    """The pill is suppressed on content, so the ``write_todos`` of a running turn is what
    brings it on screen. Both readers have to see it: one subscribed binding is enough to
    apply the crowded modifier to a row whose pill never renders.
    """
    seen = _bindings()

    assert seen["first"][0] is None, "nothing to show before the todos land"
    assert [seen["first"][-1], seen["second"][-1]] == ["1/2", "1/2"]


def test_a_streamed_arg_delta_is_not_served_from_a_stale_parse():
    """The todo list arrives as deltas on ``args``, so the parse is memoized on the segment
    *and* the string it parsed. Keyed on the segment alone, the pill would keep reporting
    whatever the first delta contained — for the tool call above, an empty list.
    """
    labels = _bindings()["first"]

    assert labels[-1] == "1/2", f"the pill never caught up with the streamed args: {labels}"
