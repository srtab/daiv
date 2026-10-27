"""Drive ``session-sync.js``'s real Alpine state under Node.

A page that rendered idle has no run to poll; ``running-start`` moves the nav store's latest running
start, which is how such a page hears that a run started after it rendered.
"""

from __future__ import annotations

import pytest

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

pytestmark = requires_node

SESSION_SYNC_JS = DAIV_DIR / "sessions/static/sessions/js/session-sync.js"

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, args, steps } = JSON.parse(readFileSync(0, "utf8"));
const components = {};
const listeners = {};
const events = [];
const watchers = [];
const fetches = [];
let reloads = 0;
let now = 0;
let timers = [];
let nextId = 1;
const responses = [];
const pending = [];
let hold = false;

const sandbox = {
  document: { addEventListener: (name, cb) => { if (name === "alpine:init") cb(); } },
  performance: { now: () => now },
  console: { warn() {} },
  CustomEvent: class {
    constructor(type, init) { this.type = type; this.detail = init?.detail; this.defaultPrevented = false; }
    preventDefault() { this.defaultPrevented = true; }
  },
  setInterval: (fn, ms) => { const id = nextId++; timers.push({ id, fn, ms, at: now + ms }); return id; },
  clearInterval: (id) => { timers = timers.filter((t) => t.id !== id); },
  location: { reload: () => { reloads += 1; } },
  fetch: (url) => {
    fetches.push(url);
    const reply = (body) => ({ ok: true, status: 200, json: async () => body });
    if (!hold) return Promise.resolve(reply(responses.shift()));
    return new Promise((resolve) => pending.push((body) => resolve(reply(body))));
  },
};
sandbox.window = sandbox;
sandbox.addEventListener = (name, cb) => { listeners[name] = cb; };
sandbox.dispatchEvent = (event) => {
  events.push({ type: event.type, detail: event.detail });
  if (listeners[event.type]) listeners[event.type](event);
  return !event.defaultPrevented;
};
sandbox.Alpine = { data: (name, factory) => { components[name] = factory; } };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const sync = components.sessionSync(args);
sync.$watch = (expression, callback) => watchers.push({ expression, callback });
sync.init();
listeners["daiv:live-run-ended"] = () => sync.resume();

const settle = () => new Promise((resolve) => setImmediate(resolve));
for (const [action, value] of steps) {
  if (action === "respond") responses.push(value);
  else if (action === "hold") hold = true;
  else if (action === "release") pending[value[0]](value[1]);
  else if (action === "hold-reload") listeners["daiv:session-settled"] = (event) => event.preventDefault();
  else if (action === "running-start") for (const watcher of watchers) watcher.callback(value);
  else if (action === "event") sandbox.dispatchEvent(new sandbox.CustomEvent(value, { detail: {} }));
  else if (action === "advance") {
    const end = now + value;
    for (;;) {
      const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      now = due.at;
      due.at += due.ms;
      due.fn();
      await settle();
    }
    now = end;
  }
  await settle();
}
process.stdout.write(
  JSON.stringify({ events, fetches: fetches.length, reloads, watched: watchers.map((w) => w.expression) }),
);
"""

TURNS_URL = "/api/sessions/t1/turns"


def _run(args: dict, steps: list) -> dict:
    return run_node(
        HARNESS, {"src": str(SESSION_SYNC_JS), "args": {"turnsUrl": TURNS_URL, "active": True, **args}, "steps": steps}
    )


def test_it_stays_quiet_while_a_live_run_streams():
    result = _run({"liveRunId": "r1"}, [["advance", 15000]])

    assert result["fetches"] == 0
    assert result["events"] == []


def test_each_turns_event_says_when_its_request_was_sent():
    """The session page drops a watch read before its own toggle answered, so it needs the poll's send time."""
    payload = {"active": True, "live_run": None, "turns": [], "watch": None}

    result = _run({"liveRunId": ""}, [["hold", None], ["advance", 5000], ["advance", 2000], ["release", [0, payload]]])

    assert result["events"][0]["detail"] == {**payload, "requestedAt": 5000}


def test_a_new_holder_is_handed_to_the_chat():
    payload = {"active": True, "live_run": {"id": "r2", "stoppable": False}, "turns": []}

    result = _run({"liveRunId": ""}, [["respond", payload], ["advance", 5000], ["advance", 15000]])

    assert [e["type"] for e in result["events"]] == ["daiv:session-turns", "daiv:session-live-run"]
    assert result["events"][1]["detail"] == {"id": "r2", "stoppable": False}
    assert result["fetches"] == 1


def test_a_poll_landing_after_the_handoff_is_dropped():
    handoff = {"active": True, "live_run": {"id": "r2", "stoppable": True}, "turns": []}
    late = {"active": True, "live_run": None, "turns": [{"id": "stale"}]}

    result = _run(
        {"liveRunId": ""},
        [["hold", None], ["advance", 5000], ["advance", 5000], ["release", [1, handoff]], ["release", [0, late]]],
    )

    assert [e["type"] for e in result["events"]] == ["daiv:session-turns", "daiv:session-live-run"]


def test_the_finished_run_is_not_followed_again():
    payload = {"active": True, "live_run": {"id": "r1", "stoppable": True}, "turns": []}

    result = _run(
        {"liveRunId": "r1"},
        [["event", "daiv:live-run-ended"], ["respond", payload], ["respond", payload], ["advance", 5000]],
    )

    assert "daiv:session-live-run" not in [e["type"] for e in result["events"]]
    assert result["fetches"] == 2


def test_an_idle_session_reloads():
    payload = {"active": False, "live_run": None, "turns": []}

    result = _run({"liveRunId": ""}, [["respond", payload], ["advance", 5000], ["advance", 15000]])

    assert result["reloads"] == 1
    assert result["fetches"] == 1


def test_a_settled_session_stays_when_the_chat_holds_the_reload():
    payload = {"active": False, "live_run": None, "turns": []}

    result = _run({"liveRunId": ""}, [["hold-reload", None], ["respond", payload], ["advance", 5000]])

    assert result["reloads"] == 0
    assert "daiv:session-settled" in [e["type"] for e in result["events"]]


@pytest.mark.parametrize("live_run_id", ["", "r1"])
def test_ending_the_run_polls_at_once(live_run_id):
    payload = {"active": True, "live_run": None, "turns": []}

    result = _run({"liveRunId": live_run_id}, [["respond", payload], ["event", "daiv:live-run-ended"]])

    assert result["fetches"] == 1


STARTED = "2026-10-23T13:16:20+00:00"


def _idle(steps: list) -> dict:
    return _run({"liveRunId": "", "active": False}, steps)


def test_an_idle_page_watches_the_nav_stores_latest_running_start():
    result = _idle([["advance", 15000]])

    assert result["fetches"] == 0
    assert len(result["watched"]) == 1
    assert "latestRunningStart" in result["watched"][0]


def test_a_background_run_starting_on_an_idle_page_is_followed():
    payload = {"active": True, "background": True, "live_run": {"id": "r2", "stoppable": False}, "turns": []}

    result = _idle([["respond", payload], ["running-start", STARTED], ["advance", 15000]])

    assert [e["type"] for e in result["events"]] == ["daiv:session-turns", "daiv:session-live-run"]
    assert result["fetches"] == 1


def test_a_background_run_with_no_holder_yet_is_polled():
    payload = {"active": True, "background": True, "live_run": None, "turns": []}

    result = _idle([["respond", payload], ["running-start", STARTED], ["respond", payload], ["advance", 5000]])

    assert result["fetches"] == 2


def test_a_run_starting_elsewhere_leaves_an_idle_page_alone():
    payload = {"active": False, "background": False, "live_run": None, "turns": []}

    result = _idle([["respond", payload], ["running-start", STARTED], ["advance", 15000]])

    assert result["fetches"] == 1
    assert result["events"] == []
    assert result["reloads"] == 0


def test_nothing_running_is_not_worth_a_check():
    assert _idle([["running-start", ""]])["fetches"] == 0


@pytest.mark.parametrize("live_run_id", ["", "r1"])
def test_an_engaged_page_does_not_check(live_run_id):
    result = _run({"liveRunId": live_run_id}, [["running-start", STARTED]])

    assert result["fetches"] == 0


def test_an_idle_page_ignores_its_own_chat_turn_ending():
    result = _idle([["event", "daiv:live-run-ended"], ["advance", 15000]])

    assert result["fetches"] == 0
    assert result["reloads"] == 0


def test_a_page_that_held_its_reload_wakes_for_the_next_background_run():
    settled = {"active": False, "live_run": None, "turns": []}
    resumed = {"active": True, "background": True, "live_run": {"id": "r3", "stoppable": False}, "turns": []}

    result = _run(
        {"liveRunId": ""},
        [
            ["hold-reload", None],
            ["respond", settled],
            ["advance", 5000],
            ["respond", resumed],
            ["running-start", STARTED],
        ],
    )

    assert result["events"][-1] == {"type": "daiv:session-live-run", "detail": resumed["live_run"]}
    assert result["reloads"] == 0
