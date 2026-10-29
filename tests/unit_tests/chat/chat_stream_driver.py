"""Boot ``chat-stream.js`` under node, so a suite only writes what it does to the module.

Several suites drive the real module, and they need the same thing to get to a component: a
`vm` context carrying enough of the browser for the file to evaluate, plus the `alpine:init`
dispatch that registers `chat` in ``registry``. Only the stubs each one *adds* differ, so the
boot lives here: a global the module starts reaching for is then stubbed once, not once per
suite. The transcript modules the page loads before it (labels, tool rows, run blocks) are
booted first, as the page orders them.
"""

from __future__ import annotations

from typing import Any

from tests.unit_tests.jsdriver import run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

JS_DIR = DAIV_DIR / "chat" / "static" / "chat" / "js"
CHAT_STREAM_JS = JS_DIR / "chat-stream.js"
TRANSCRIPT_LABELS_JS = JS_DIR / "transcript-labels.js"
TOOL_RENDERERS_JS = JS_DIR / "tool-renderers.js"
TRANSCRIPT_BLOCKS_JS = JS_DIR / "transcript-blocks.js"

DIFF_STUB = """{
  diffLines: (a, b) => {
    const count = (s) => (s ? s.split("\\n").length - (s.endsWith("\\n") ? 1 : 0) : 0);
    return [{ removed: true, count: count(a) }, { added: true, count: count(b) }];
  },
}"""

REACTIVITY = """
/* A stand-in for Alpine's reactivity, small enough to read: per-effect dependency tracking
   over proxies, effects run synchronously. Synchronous is the point — Alpine's initial tree
   walk runs every binding in one task, and its scheduler flushes the whole queued batch
   inside one microtask, so a value cached "for the rest of the microtask" is shared by
   every reader of the flush and only the first of them is ever asked what it read. */
let active = null;
let activeRun = 0;
let runs = 0;
let onTrack = null;
const deps = new WeakMap();
const proxies = new WeakMap();
const isProxy = new WeakSet();

const track = (target, key) => {
  if (onTrack) onTrack(target, key);
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
  if (!value || typeof value !== "object" || isProxy.has(value)) return value;
  // Alpine proxies collections with handlers of their own; a plain proxy breaks `Map.prototype.size`.
  if (/^\\[object (Map|Set)\\]$/.test(Object.prototype.toString.call(value))) return value;
  if (proxies.has(value)) return proxies.get(value);
  const proxy = new Proxy(value, {
    get(target, key, receiver) {
      track(target, key);
      return reactive(Reflect.get(target, key, receiver));
    },
    set(target, key, next, receiver) {
      const changed = !Object.hasOwn(target, key) || !Object.is(target[key], next);
      const accepted = Reflect.set(target, key, next, receiver);
      if (changed) {
        trigger(target, key);
        trigger(target, "length");
      }
      return accepted;
    },
  });
  proxies.set(value, proxy);
  isProxy.add(proxy);
  return proxy;
};

const effect = (fn) => {
  const run = () => {
    const previous = active;
    const previousRun = activeRun;
    active = run;
    activeRun = ++runs;
    try { fn(); } finally { active = previous; activeRun = previousRun; }
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

_HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const payload = JSON.parse(readFileSync(0, "utf8"));
const registry = {};
// The reactivity half of Alpine's API, which `init()` uses to arm the transcript effect.
// A suite that brings a reactivity stand-in reassigns `alpineEffect` from its prelude; the
// default runs the body once, which is all an untracked read amounts to.
let alpineEffect = (fn) => { fn(); return fn; };
const dispatched = [];
// __PRELUDE__
const sandbox = {
  console: { log: () => {}, debug: () => {}, warn: () => {}, error: () => {} },
  // `queueMicrotask` is not reached by the module as it stands; it is here so that a
  // reintroduced per-flush cache fails on what a suite asserts rather than on a missing
  // global, which reads as a broken harness instead of the regression it is.
  crypto, setInterval, clearInterval, queueMicrotask, AbortController,
  CSS: { supports: () => true },
  document: {
    getElementById: () => null,
    querySelector: () => null,
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); },
  },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  window: {
    dispatchEvent: (event) => { dispatched.push(event); return true; },
    location: { hash: "" },
    Diff: __DIFF_STUB__,
    Alpine: {
      data: (name, factory) => (registry[name] = factory),
      effect: (fn) => alpineEffect(fn),
      release: () => {},
    },
  },
  // __GLOBALS__
};
vm.createContext(sandbox);
for (const src of payload.sources) vm.runInContext(readFileSync(src, "utf8"), sandbox);
// __BODY__
""".replace("__DIFF_STUB__", DIFF_STUB)

_SOURCES = [TRANSCRIPT_LABELS_JS, TOOL_RENDERERS_JS, TRANSCRIPT_BLOCKS_JS, CHAT_STREAM_JS]


def run_chat_stream(body: str, payload: dict, *, prelude: str = "", extra_globals: str = "") -> Any:
    """Run ``body`` against a booted ``chat-stream.js``; return what it wrote to stdout.

    ``body`` reads its inputs off ``payload`` and the component factory off ``registry.chat``.
    ``prelude`` lands above the sandbox, for stubs its ``extra_globals`` entries name, and is
    also where a suite puts ``REACTIVITY`` (or its own stand-in) to reassign ``alpineEffect``.
    """
    harness = _HARNESS.replace("// __PRELUDE__", prelude)
    harness = harness.replace("// __GLOBALS__", extra_globals).replace("// __BODY__", body)
    return run_node(harness, {"sources": [str(src) for src in _SOURCES], **payload})
