"""Behavioral tests for the transcript poller in ``sessions/js/session-sync.js``, driven under node.

The poller's hard case is a background run that starts after the page rendered, such as a
delegation resume or a CI fix, so these tests feed it the nav store's changes and the turns
endpoint's answers, and read back what it fetched, dispatched and armed.
"""

from __future__ import annotations

import pytest

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

SESSION_SYNC_JS = DAIV_DIR / "sessions" / "static" / "sessions" / "js" / "session-sync.js"

pytestmark = requires_node

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, props, responses, starts } = JSON.parse(readFileSync(0, "utf8"));
const components = {};
const fetched = [];
const dispatched = [];
const watchers = [];
let armed = 0;

const sandbox = {
  console: { warn: () => {} },
  fetch: async (url) => {
    fetched.push(url);
    const body = responses.shift();
    return { ok: true, json: async () => body };
  },
  setInterval: () => (armed += 1),
  clearInterval: () => {},
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
  location: { reload: () => {} },
  document: { addEventListener: (name, cb) => { if (name === "alpine:init") cb(); } },
};
sandbox.window = sandbox;
sandbox.dispatchEvent = (event) => dispatched.push(event.type);
sandbox.Alpine = { data: (name, factory) => (components[name] = factory) };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const poller = components.sessionSync(props);
poller.$watch = (expression, callback) => watchers.push({ expression, callback });
poller.init();
let previous = starts[0];
for (const next of starts.slice(1)) {
  for (const watcher of watchers) watcher.callback(next, previous);
  previous = next;
  await new Promise((resolve) => setImmediate(resolve));
}

process.stdout.write(JSON.stringify({
  fetched: fetched.length,
  dispatched,
  armed,
  watched: watchers.map((w) => w.expression),
}));
"""

TURNS_URL = "/api/sessions/t1/turns"


def drive(*, active: bool, starts: list[str], responses: list[dict] | None = None) -> dict:
    """Mount the poller, then move the nav store's latest running start through ``starts``."""
    payload = {
        "src": str(SESSION_SYNC_JS),
        "props": {"turnsUrl": TURNS_URL, "active": active},
        "starts": starts,
        "responses": responses or [],
    }
    return run_node(HARNESS, payload)


def _turns(*, background: bool) -> dict:
    return {"turns": [{"id": "u1", "role": "user", "segments": []}], "active": background, "background": background}


def test_it_watches_the_nav_stores_latest_running_start():
    state = drive(active=False, starts=[""])

    assert len(state["watched"]) == 1
    assert "latestRunningStart" in state["watched"][0]


def test_a_run_starting_on_this_session_arms_the_poller():
    state = drive(active=False, starts=["", "2026-10-22T13:16:20+00:00"], responses=[_turns(background=True)])

    assert state["fetched"] == 1
    assert state["dispatched"] == ["daiv:session-turns"]
    assert state["armed"] == 1


def test_a_run_starting_elsewhere_leaves_the_page_alone():
    state = drive(active=False, starts=["", "2026-10-22T13:16:20+00:00"], responses=[_turns(background=False)])

    assert state["fetched"] == 1
    assert state["dispatched"] == []
    assert state["armed"] == 0


def test_nothing_running_is_not_worth_a_check():
    state = drive(active=False, starts=["2026-10-22T13:16:20+00:00", ""])

    assert state["fetched"] == 0


@pytest.mark.parametrize("active", [True, False])
def test_a_poller_already_running_does_not_check_again(active):
    starts = ["", "2026-10-22T13:16:20+00:00", "2026-10-22T13:17:00+00:00"]

    state = drive(active=active, starts=starts, responses=[_turns(background=True), _turns(background=True)])

    assert state["armed"] == 1
    assert state["fetched"] == (0 if active else 1)
