"""Drive ``session-sync.js``'s real Alpine state under Node."""

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
  console: { warn() {} },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
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
  return true;
};
sandbox.Alpine = { data: (name, factory) => { components[name] = factory; } };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const sync = components.sessionSync(args);
sync.init();

const settle = () => new Promise((resolve) => setImmediate(resolve));
for (const [action, value] of steps) {
  if (action === "respond") responses.push(value);
  else if (action === "hold") hold = true;
  else if (action === "release") pending[value[0]](value[1]);
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
process.stdout.write(JSON.stringify({ events, fetches: fetches.length, reloads }));
"""

TURNS_URL = "/api/sessions/t1/turns"


def _run(args: dict, steps: list) -> dict:
    return run_node(HARNESS, {"src": str(SESSION_SYNC_JS), "args": {"turnsUrl": TURNS_URL, **args}, "steps": steps})


def test_it_stays_quiet_while_a_live_run_streams():
    result = _run({"liveRunId": "r1"}, [["advance", 15000]])

    assert result["fetches"] == 0
    assert result["events"] == []


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


@pytest.mark.parametrize("live_run_id", ["", "r1"])
def test_ending_the_run_polls_at_once(live_run_id):
    payload = {"active": True, "live_run": None, "turns": []}

    result = _run({"liveRunId": live_run_id}, [["respond", payload], ["event", "daiv:live-run-ended"]])

    assert result["fetches"] == 1
