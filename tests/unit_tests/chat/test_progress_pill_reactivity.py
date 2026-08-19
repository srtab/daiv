"""Behavioral tests for the composer's progress-pill state under Alpine's reactivity.

Around fifteen always-mounted bindings read ``latestTodos`` / ``filesTouched`` / the pill
built from them — the pill's ``x-show``, its tone class, the live dot, the label, the
crowded modifier on the action row, and the sheet's two lists and counts (the sheet is
``x-show``-mounted, so they stay live while it is closed). Two failures live in how that
state is derived, and neither is visible to a source-string assertion:

*Nothing updates.* A getter that caches its walk of the transcript across readers answers
every binding after the first without touching ``turns``. Alpine tracks dependencies per
effect, so those bindings subscribe to nothing and never run again — which is what kept
the pill hidden until a reload when a turn's ``write_todos`` landed mid-run.

*Everything is derived fifteen times.* A getter that re-walks per read pays a full
transcript pass per binding — twenty per streamed ``args`` delta, seconds of main thread
over a long run.

One effect armed in ``init()`` is what satisfies both, so these drive the real module
against a stand-in for the reactivity, and run the real ``init()`` to arm it.
"""

from __future__ import annotations

from tests.unit_tests.chat.chat_stream_driver import run_chat_stream
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

TODOS = [
    {"content": "Define audit sub-areas from repo layout", "status": "completed"},
    {"content": "Launch parallel audit subagents per sub-area", "status": "in_progress"},
]

PRELUDE = """
/* A stand-in for Alpine's reactivity, small enough to read: per-effect dependency tracking
   over proxies, effects run synchronously. Synchronous is the point — Alpine's initial tree
   walk runs every binding in one task, and its scheduler flushes the whole queued batch
   inside one microtask, so a value cached "for the rest of the microtask" is shared by
   every reader of the flush and only the first of them is ever asked what it read. */
let active = null;
const deps = new WeakMap();
const proxies = new WeakMap();

/* Counts walks of the whole transcript: `probe` is one segment of an *earlier* turn, whose
   `type` only the walk that collects files touched ever reads. */
let probe = null;
let walks = 0;

const track = (target, key) => {
  if (target === probe && key === "type") walks++;
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

alpineEffect = effect;

/* `$watch(expr, cb)` takes the expression as a string, as Alpine does, and fires on a
   change of its value — which is why the transcript effect can't be a `$watch`: a mutation
   inside `turns` leaves the expression's value identical. */
const watchOn = (state) => (expr, cb) => {
  const read = new Function("s", `with (s) { return (${expr}); }`);
  let previous;
  let first = true;
  effect(() => {
    const value = read(state);
    if (first) { first = false; previous = value; return; }
    if (value !== previous) { const stale = previous; previous = value; cb(value, stale); }
  });
};
"""

BODY = """
const state = reactive(registry.chat({ endpoint: "/api/chat" }));
state.$watch = watchOn(state);
// `init()`'s only `$nextTick` parks the viewport, which nothing here has.
state.$nextTick = () => {};

// History behind the open turn, the way a long run has it. The probe sits in that history,
// so a read of it counts one walk of the whole transcript.
const history = { role: "assistant", streaming: false, segments: [
  { id: "w-1", type: "tool_call", name: "write_file", status: "done",
    args: JSON.stringify({ path: "daiv/settings.py" }), result: "ok" },
] };
probe = history.segments[0];
state.turns = [{ role: "user", segments: [{ type: "text", content: "audit this" }] }, history];
state.init();

state.streaming = true;
state.turns.push({ role: "assistant", streaming: true, segments: [] });

// What the composer mounts on this state. Each pill reader records what it saw, so a
// binding that stopped re-running is visible in its own history rather than in a total.
const seen = { first: [], second: [] };
effect(() => { seen.first.push(state.progressPill?.label ?? null); });
effect(() => { seen.second.push(state.progressPill?.label ?? null); });
for (let i = 0; i < 5; i++) effect(() => { state.progressPill; });
for (let i = 0; i < 6; i++) effect(() => { state.filesTouched.length; });
for (let i = 0; i < 3; i++) effect(() => { state.latestTodos.length; });
effect(() => { state.todosProgress; });

// The agent calls write_todos mid-turn: the tool call is appended to the open turn, then
// its args stream in — split, because a delta arrives per relay frame.
const turn = state.turns[state.turns.length - 1];
turn.segments.push({ id: "tc-1", type: "tool_call", name: "write_todos", args: "", status: "running" });
const live = turn.segments[0];
const args = JSON.stringify({ todos: payload.todos });

walks = 0;
live.args += args.slice(0, 12);
const perDelta = walks;
live.args += args.slice(12);

const result = { seen, perDelta, total: walks };
state.destroy();
process.stdout.write(JSON.stringify(result));
"""


def _run() -> dict:
    """Stream a mid-turn ``write_todos`` into a mounted composer; report what it cost."""
    return run_chat_stream(
        BODY, {"todos": TODOS}, prelude=PRELUDE, extra_globals="surfaceGroup: { join: () => () => {} },"
    )


def test_every_binding_sees_the_todos_that_land_mid_turn():
    """The pill is suppressed on content, so the ``write_todos`` of a running turn is what
    brings it on screen. Both readers have to see it: one subscribed binding is enough to
    apply the crowded modifier to a row whose pill never renders.

    The list arrives as deltas on ``args``, so this also pins the memo key: parses are
    cached on the segment *and* the string parsed. Keyed on the segment alone, the pill
    would keep reporting whatever the first delta held — here, an empty list.
    """
    seen = _run()["seen"]

    assert seen["first"][0] == "1 file", "the history's write_file is all the pill has yet"
    assert [seen["first"][-1], seen["second"][-1]] == ["1/2 · 1 file", "1/2 · 1 file"]


def test_a_streamed_delta_walks_the_transcript_once():
    """The cost half. Every binding above reads state derived by the one effect ``init()``
    arms, so a delta walks the transcript once no matter how many bindings are mounted —
    derive per read instead and this is one full pass per reader, twenty here, on every
    frame of a run whose transcript only grows.
    """
    walks = _run()

    assert walks["perDelta"] == 1, f"a single delta walked the transcript {walks['perDelta']} times"
    assert walks["total"] == 2, f"two deltas, two walks — got {walks['total']}"
