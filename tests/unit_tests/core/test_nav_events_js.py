"""Behavioral tests for the `nav` Alpine store in ``core/js/nav-events.js``.

Driven under node (stubbing only Alpine and ``EventSource``) because the store's whole
job is reconciling what a source-string assertion cannot see: the server-rendered seed, the
frames that arrive later, and which of them the page has to hear about.
"""

from __future__ import annotations

import json

import pytest

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

NAV_EVENTS_JS = DAIV_DIR / "core" / "static" / "core" / "js" / "nav-events.js"

pytestmark = requires_node

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, props, starts, frames, dispatch, sessionFrames, visibility } = JSON.parse(readFileSync(0, "utf8"));
const stores = {};
let opened = null;

let constructed = 0;
const dispatched = [];
let warnings = 0;

class FakeCustomEvent {
  constructor(type, init = {}) { this.type = type; this.detail = init.detail; }
}

class FakeEventSource {
  constructor(url) { this.url = url; this.listeners = {}; this.closed = false; opened = this; constructed += 1; }
  addEventListener(name, cb) { this.listeners[name] = cb; }
  close() { this.closed = true; }
}

const docListeners = {};
const sandbox = {
  console: { warn: () => { warnings += 1; }, error: () => {} },
  CustomEvent: FakeCustomEvent,
  dispatchEvent: (event) => dispatched.push({ type: event.type, detail: event.detail }),
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
for (const data of sessionFrames) opened.listeners.session({ data });
for (const state of visibility) {
  sandbox.document.visibilityState = state;
  docListeners.visibilitychange();
}

process.stdout.write(JSON.stringify({
  unread: nav.unread,
  running: nav.running,
  url: opened && opened.url,
  closed: opened ? opened.closed : null,
  constructed,
  dispatched,
  warnings,
}));
"""


def drive(
    seed: dict | None = None,
    frames: list[dict] | None = None,
    dispatch: list[str] | None = None,
    session_frames: list[str] | None = None,
    visibility: list[str] | None = None,
    starts: int = 1,
) -> dict:
    """Seed the store as ``base_app.html`` does, then feed it ``frames``.

    ``seed`` takes the server's snapshot keys, not the store's field names — that is the
    shape the template renders, and the point is that it is the frames' shape too.
    """
    payload = {
        "src": str(NAV_EVENTS_JS),
        "props": {
            "url": "/api/nav/events",
            "state": {"unread_count": 0, "running_runs": 0, "own_running_runs": 0, **(seed or {})},
        },
        "starts": starts,
        "frames": frames or [],
        "dispatch": dispatch or [],
        "sessionFrames": session_frames or [],
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
    """Clearing the unread badge is the common case ("Mark all as read"), so a
    falsy-but-present count must not be skipped."""
    state = drive({"unread_count": 7}, frames=[{"unread_count": 0, "running_runs": 0}])
    assert state["unread"] == 0


def test_a_partial_snapshot_leaves_the_other_count_alone():
    state = drive({"unread_count": 3, "running_runs": 2}, frames=[{"unread_count": 1}])
    assert (state["unread"], state["running"]) == (1, 2)


def test_an_unreadable_frame_is_ignored_rather_than_clearing_the_badges():
    state = drive({"unread_count": 3, "running_runs": 2}, frames=[{"unread_count": "nonsense"}])
    assert (state["unread"], state["running"]) == (3, 2)


def test_starting_twice_does_not_open_a_second_stream():
    """`start` must be idempotent per tab: a second call would leave a duplicate connection
    nobody can close."""
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


@pytest.mark.parametrize("revision", [3, 0])
def test_a_session_frame_is_announced_to_the_page(revision):
    """The bus reads a missing key as 0, so 0 is a real revision the page must fetch against."""
    state = drive(session_frames=[json.dumps({"revision": revision})])
    assert state["dispatched"] == [{"type": "daiv:session-changed", "detail": {"revision": revision}}]


def test_every_session_frame_is_announced_in_order():
    state = drive(session_frames=['{"revision": 4}', '{"revision": 5}'])
    assert [event["detail"]["revision"] for event in state["dispatched"]] == [4, 5]


@pytest.mark.parametrize(
    "data", ["not json", "null", "3", "{}", '{"revision": "3"}', '{"revision": 1.5}', '{"revision": null}']
)
def test_an_unreadable_session_frame_is_ignored(data):
    state = drive({"unread_count": 3, "running_runs": 2}, session_frames=[data])
    assert state["dispatched"] == []
    assert state["warnings"] == 1
    assert (state["unread"], state["running"]) == (3, 2)
    assert state["closed"] is False


def test_a_bad_session_frame_does_not_stop_the_next_one():
    state = drive(session_frames=["not json", '{"revision": 7}'])
    assert [event["detail"]["revision"] for event in state["dispatched"]] == [7]


def _runs_changed(state) -> int:
    return [event["type"] for event in state["dispatched"]].count("daiv:runs-changed")


def test_the_seed_and_a_frame_that_matches_it_announce_nothing():
    state = drive(
        {"own_running_runs": 2}, frames=[{"unread_count": 0, "own_running_runs": 2, "own_latest_running_start": "t1"}]
    )
    assert _runs_changed(state) == 0


@pytest.mark.parametrize(
    "frame",
    [
        {"own_running_runs": 3, "own_latest_running_start": "t1"},
        {"own_running_runs": 2, "own_latest_running_start": "t2"},
    ],
)
def test_a_frame_that_moves_the_own_running_snapshot_announces_it(frame):
    """The start moves when one run starts as another finishes, which leaves the count alone."""
    state = drive(
        {"own_running_runs": 2},
        frames=[{"own_running_runs": 2, "own_latest_running_start": "t1"}, {"unread_count": 0, **frame}],
    )
    assert _runs_changed(state) == 1


def test_runs_the_user_only_sees_announce_nothing():
    """The lists show only the user's own sessions, so a teammate's run cannot change them."""
    state = drive(
        {"running_runs": 1, "own_running_runs": 1},
        frames=[
            {"running_runs": 1, "latest_running_start": "t1", "own_running_runs": 1, "own_latest_running_start": "t1"},
            {"running_runs": 3, "latest_running_start": "t9", "own_running_runs": 1, "own_latest_running_start": "t1"},
        ],
    )
    assert state["running"] == 3
    assert _runs_changed(state) == 0


def test_an_unread_change_alone_announces_nothing():
    state = drive({"own_running_runs": 1}, frames=[{"unread_count": 4, "own_running_runs": 1}])
    assert _runs_changed(state) == 0


def test_coming_back_into_view_announces_once():
    """Runs may have started and finished while the stream was closed, unseen."""
    state = drive({}, visibility=["hidden", "visible"])
    assert _runs_changed(state) == 1
