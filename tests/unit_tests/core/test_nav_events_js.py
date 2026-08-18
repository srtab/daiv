"""Behavioral tests for the `nav` Alpine store in ``core/js/nav-events.js``.

Driven under node (stubbing only Alpine and ``EventSource``) because the store's whole
job is reconciling three things a source-string assertion cannot see: the server-rendered
seed, the frames that arrive later, and a label whose number is interpolated client-side.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404
from pathlib import Path

import pytest

NAV_EVENTS_JS = Path(__file__).resolve().parents[3] / "daiv" / "core" / "static" / "core" / "js" / "nav-events.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to drive nav-events.js")

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, props, frames, dispatch } = JSON.parse(readFileSync(0, "utf8"));
const stores = {};
let opened = null;

let constructed = 0;

class FakeEventSource {
  constructor(url) { this.url = url; this.listeners = {}; this.closed = false; opened = this; constructed += 1; }
  addEventListener(name, cb) { this.listeners[name] = cb; }
  close() { this.closed = true; }
}

const sandbox = {
  console: { warn: () => {}, error: () => {} },
  EventSource: FakeEventSource,
  document: { addEventListener: (name, cb) => { if (name === "alpine:init") cb(); } },
};
sandbox.window = sandbox;
sandbox.Alpine = { store: (name, value) => (stores[name] = value) };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const nav = stores.nav;
nav.start(props);
if (props.startTwice) nav.start(props);
for (const frame of frames) opened.listeners.snapshot({ data: JSON.stringify(frame) });
for (const name of dispatch) opened.listeners[name]({ data: "{}" });

process.stdout.write(JSON.stringify({
  unread: nav.unread,
  running: nav.running,
  runningLabel: nav.runningLabel,
  url: opened && opened.url,
  closed: opened ? opened.closed : null,
  constructed,
}));
"""


def drive(props: dict, frames: list[dict] | None = None, dispatch: list[str] | None = None) -> dict:
    payload = {
        "src": str(NAV_EVENTS_JS),
        "props": {"url": "/api/nav/events", "unread": 0, "running": 0, "runningLabel": "%(count)s running", **props},
        "frames": frames or [],
        "dispatch": dispatch or [],
    }
    proc = subprocess.run(  # noqa: S603
        [str(NODE), "--input-type=module", "-e", HARNESS],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_seeds_from_the_page_render_and_connects():
    state = drive({"unread": 3, "running": 2})
    assert (state["unread"], state["running"]) == (3, 2)
    assert state["url"] == "/api/nav/events"


def test_a_snapshot_replaces_both_counts():
    state = drive({"unread": 3, "running": 2}, frames=[{"unread_count": 0, "running_runs": 5}])
    assert (state["unread"], state["running"]) == (0, 5)


def test_a_zero_snapshot_is_applied_not_treated_as_missing():
    """Clearing the bell is the common case (opening the dropdown marks everything read),
    so a falsy-but-present count must not be skipped."""
    state = drive({"unread": 7}, frames=[{"unread_count": 0, "running_runs": 0}])
    assert state["unread"] == 0


def test_a_partial_snapshot_leaves_the_other_count_alone():
    state = drive({"unread": 3, "running": 2}, frames=[{"unread_count": 1}])
    assert (state["unread"], state["running"]) == (1, 2)


def test_an_unreadable_frame_is_ignored_rather_than_clearing_the_badges():
    state = drive({"unread": 3, "running": 2}, frames=[{"unread_count": "nonsense"}])
    assert (state["unread"], state["running"]) == (3, 2)


def test_the_label_interpolates_the_live_count():
    """The number cannot go through `blocktranslate`, so the store fills the placeholder
    the server left in the translated sentence."""
    state = drive({"running": 4, "runningLabel": "%(count)s em execução"})
    assert state["runningLabel"] == "4 em execução"


def test_the_label_follows_a_snapshot():
    state = drive({"running": 1}, frames=[{"unread_count": 0, "running_runs": 9}])
    assert state["runningLabel"] == "9 running"


def test_starting_twice_does_not_open_a_second_stream():
    """Both sidebar copies and the bell share one store; a second `start` would leave a
    duplicate connection nobody can close."""
    state = drive({"startTwice": True})
    assert state["closed"] is False
    assert state["constructed"] == 1


def test_an_end_frame_closes_the_stream():
    """The server sends `end` only after giving up, so reconnecting would hammer a broken
    backend — the badges hold their last values until the next page load."""
    state = drive({"unread": 2}, dispatch=["end"])
    assert state["closed"] is True
    assert state["unread"] == 2
