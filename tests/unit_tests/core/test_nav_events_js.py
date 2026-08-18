"""Behavioral tests for the `nav` Alpine store in ``core/js/nav-events.js``.

Driven under node (stubbing only Alpine and ``EventSource``) because the store's whole
job is reconciling three things a source-string assertion cannot see: the server-rendered
seed, the frames that arrive later, and a label whose number is interpolated client-side.
"""

from __future__ import annotations

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

NAV_EVENTS_JS = DAIV_DIR / "core" / "static" / "core" / "js" / "nav-events.js"

pytestmark = requires_node

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, props, starts, frames, dispatch, visibility } = JSON.parse(readFileSync(0, "utf8"));
const stores = {};
let opened = null;

let constructed = 0;

class FakeEventSource {
  constructor(url) { this.url = url; this.listeners = {}; this.closed = false; opened = this; constructed += 1; }
  addEventListener(name, cb) { this.listeners[name] = cb; }
  close() { this.closed = true; }
}

const docListeners = {};
const sandbox = {
  console: { warn: () => {}, error: () => {} },
  EventSource: FakeEventSource,
  document: {
    visibilityState: "visible",
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); else docListeners[name] = cb; },
  },
};
sandbox.window = sandbox;
sandbox.Alpine = { store: (name, value) => (stores[name] = value) };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const nav = stores.nav;
for (let i = 0; i < starts; i++) nav.start(props);
for (const frame of frames) opened.listeners.snapshot({ data: JSON.stringify(frame) });
for (const name of dispatch) opened.listeners[name]({ data: "{}" });
for (const state of visibility) {
  sandbox.document.visibilityState = state;
  docListeners.visibilitychange();
}

process.stdout.write(JSON.stringify({
  unread: nav.unread,
  running: nav.running,
  runningLabel: nav.runningLabel,
  url: opened && opened.url,
  closed: opened ? opened.closed : null,
  constructed,
}));
"""


def drive(
    seed: dict | None = None,
    frames: list[dict] | None = None,
    dispatch: list[str] | None = None,
    visibility: list[str] | None = None,
    starts: int = 1,
    running_label: str = "{count} running",
) -> dict:
    """Seed the store as ``base_app.html`` does, then feed it ``frames``.

    ``seed`` takes the server's snapshot keys, not the store's field names — that is the
    shape the template renders, and the point is that it is the frames' shape too.
    """
    payload = {
        "src": str(NAV_EVENTS_JS),
        "props": {
            "url": "/api/nav/events",
            "state": {"unread_count": 0, "running_runs": 0, **(seed or {})},
            "runningLabel": running_label,
        },
        "starts": starts,
        "frames": frames or [],
        "dispatch": dispatch or [],
        "visibility": visibility or [],
    }
    return run_node(HARNESS, payload)


def test_seeds_from_the_page_render_and_connects():
    state = drive({"unread_count": 3, "running_runs": 2})
    assert (state["unread"], state["running"]) == (3, 2)
    assert state["url"] == "/api/nav/events"


def test_a_snapshot_replaces_both_counts():
    state = drive({"unread_count": 3, "running_runs": 2}, frames=[{"unread_count": 0, "running_runs": 5}])
    assert (state["unread"], state["running"]) == (0, 5)


def test_a_zero_snapshot_is_applied_not_treated_as_missing():
    """Clearing the bell is the common case (opening the dropdown marks everything read),
    so a falsy-but-present count must not be skipped."""
    state = drive({"unread_count": 7}, frames=[{"unread_count": 0, "running_runs": 0}])
    assert state["unread"] == 0


def test_a_partial_snapshot_leaves_the_other_count_alone():
    state = drive({"unread_count": 3, "running_runs": 2}, frames=[{"unread_count": 1}])
    assert (state["unread"], state["running"]) == (1, 2)


def test_an_unreadable_frame_is_ignored_rather_than_clearing_the_badges():
    state = drive({"unread_count": 3, "running_runs": 2}, frames=[{"unread_count": "nonsense"}])
    assert (state["unread"], state["running"]) == (3, 2)


def test_the_label_interpolates_the_live_count():
    """The number cannot go through `blocktranslate`, so the store fills the placeholder
    the server left in the translated sentence."""
    state = drive({"running_runs": 4}, running_label="{count} em execução")
    assert state["runningLabel"] == "4 em execução"


def test_the_label_follows_a_snapshot():
    state = drive({"running_runs": 1}, frames=[{"unread_count": 0, "running_runs": 9}])
    assert state["runningLabel"] == "9 running"


def test_starting_twice_does_not_open_a_second_stream():
    """Both sidebar copies and the bell share one store; a second `start` would leave a
    duplicate connection nobody can close."""
    state = drive({}, starts=2)
    assert state["closed"] is False
    assert state["constructed"] == 1


def test_an_end_frame_closes_the_stream():
    """The server sends `end` only after giving up, so reconnecting would hammer a broken
    backend — the badges hold their last values until the next page load."""
    state = drive({"unread_count": 2}, dispatch=["end"])
    assert state["closed"] is True
    assert state["unread"] == 2


def test_a_backgrounded_tab_drops_the_stream():
    """The poll this replaced was gated on visibility; an ungated stream holds a worker,
    a recount per poke and one of six per-origin connections for every hidden tab."""
    state = drive({}, visibility=["hidden"])
    assert state["closed"] is True
    assert state["constructed"] == 1


def test_coming_back_into_view_reopens_it():
    """The fresh stream's first frame is a whole-state snapshot, so the gap self-heals."""
    state = drive({}, visibility=["hidden", "visible"])
    assert state["closed"] is False
    assert state["constructed"] == 2


def test_a_stream_the_server_ended_is_not_reopened_by_visibility():
    state = drive({}, dispatch=["end"], visibility=["hidden", "visible"])
    assert state["closed"] is True
    assert state["constructed"] == 1
