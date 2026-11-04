"""Drive ``session-sync.js``'s real Alpine state under Node.

``changed`` is a ``daiv:session-changed`` frame's revision and ``ended`` a ``daiv:live-run-ended`` detail; the
harness calls the component the way the template's listeners do. ``setInterval`` throws, so any polling fails a test.
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
const events = [];
const handovers = [];
const ends = [];
const fetches = [];
let reloads = 0;
let now = 0;
let timers = [];
let nextId = 1;
const responses = [];
const pending = [];
let hold = false;

const reply = ({ status, body }) =>
  status === 0 ? Promise.reject(new TypeError("offline")) : { ok: status < 400, status, json: async () => body };

const sandbox = {
  document: { addEventListener: (name, cb) => { if (name === "alpine:init") cb(); } },
  performance: { now: () => now },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  setTimeout: (fn, ms) => { const id = nextId++; timers.push({ id, fn, at: now + ms }); return id; },
  clearTimeout: (id) => { timers = timers.filter((t) => t.id !== id); },
  setInterval: () => { throw new Error("sessionSync must not poll"); },
  location: { reload: () => { reloads += 1; } },
  fetch: (url) => {
    fetches.push(url);
    if (!hold) return Promise.resolve().then(() => reply(responses.shift()));
    return new Promise((resolve) => pending.push((body) => resolve(reply({ status: 200, body }))));
  },
};
sandbox.window = sandbox;
sandbox.dispatchEvent = (event) => {
  events.push({ type: event.type, detail: event.detail });
  if (event.type === "daiv:session-live-run") handovers.push({ id: event.detail.id, at: now });
  return true;
};
sandbox.Alpine = { data: (name, factory) => { components[name] = factory; } };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const sync = components.sessionSync(args);
sync.init?.();

const settle = () => new Promise((resolve) => setImmediate(resolve));
for (const [action, value] of steps) {
  if (action === "respond") responses.push({ status: 200, body: value });
  else if (action === "fail") responses.push({ status: value });
  else if (action === "hold") hold = true;
  else if (action === "release") pending[value[0]](value[1]);
  else if (action === "changed") sync.changed({ revision: value });
  else if (action === "ended") { ends.push(now); sync.runEnded(value); }
  else if (action === "advance") {
    const end = now + value;
    for (;;) {
      const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      timers = timers.filter((t) => t !== due);
      now = due.at;
      due.fn();
      await settle();
    }
    now = end;
  }
  await settle();
}
process.stdout.write(
  JSON.stringify({ events, handovers, ends, fetches: fetches.length, reloads, timers: timers.length }),
);
"""

TURNS_URL = "/api/sessions/t1/turns"
R1 = {"id": "r1", "stoppable": True, "started_at": "2026-11-04T10:00:00+00:00"}
R2 = {"id": "r2", "stoppable": False, "started_at": "2026-11-04T10:05:00+00:00"}


def _run(steps: list, *, live_run_id: str = "", revision: int = 1) -> dict:
    args = {"turnsUrl": TURNS_URL, "liveRunId": live_run_id, "revision": revision}
    return run_node(HARNESS, {"src": str(SESSION_SYNC_JS), "args": args, "steps": steps})


def _turns(revision: int = 2, *, live_run=None, run_count: int = 1, **extra) -> dict:
    active = live_run is not None
    return {"revision": revision, "active": active, "live_run": live_run, "run_count": run_count, "turns": [], **extra}


def _types(result: dict) -> list[str]:
    return [event["type"] for event in result["events"]]


def _ended(run_id: str, reason: str, *, own: bool = False, frames: int = 0) -> list:
    return ["ended", {"runId": run_id, "reason": reason, "own": own, "frames": frames}]


def test_an_unchanged_revision_fetches_nothing():
    """The nav stream re-sends the revision on every connect; an idle tab must not pay a fetch for it."""
    result = _run([["changed", 5], ["advance", 600_000], ["changed", 5]], revision=5)

    assert (result["fetches"], result["events"]) == (0, [])


def test_a_new_revision_fetches_the_turns_once():
    payload = _turns(6)

    result = _run([["respond", payload], ["changed", 6], ["changed", 6]], revision=5)

    assert result["fetches"] == 1
    assert result["events"] == [{"type": "daiv:session-turns", "detail": {**payload, "requestedAt": 0}}]


def test_a_lower_revision_still_fetches():
    """The revision key can expire or be flushed, so the page fetches on any difference."""
    result = _run([["respond", _turns(2)], ["changed", 2]], revision=9)

    assert result["fetches"] == 1


def test_each_turns_event_says_when_its_request_was_sent():
    """The session page drops a CI watch read before its own toggle answered, so it needs the fetch's send time."""
    result = _run([["advance", 5000], ["hold", None], ["changed", 2], ["advance", 2000], ["release", [0, _turns()]]])

    assert result["events"][0]["detail"]["requestedAt"] == 5000


def test_changes_during_a_fetch_cost_exactly_one_more():
    steps = [["hold", None], ["changed", 2], ["changed", 3], ["changed", 4], ["release", [0, _turns(2)]]]

    result = _run([*steps, ["release", [1, _turns(4)]], ["changed", 4]])

    assert result["fetches"] == 2
    assert _types(result) == ["daiv:session-turns", "daiv:session-turns"]


def test_a_change_during_a_fetch_that_the_response_already_carries_costs_nothing_more():
    result = _run([["hold", None], ["changed", 2], ["changed", 3], ["release", [0, _turns(3)]]])

    assert result["fetches"] == 1


def test_a_run_ending_during_a_fetch_always_costs_one_more():
    """The run's end may not have bumped the revision yet, so a response at the announced revision can predate it."""
    steps = [["hold", None], ["changed", 2], _ended("r1", "finished", frames=4), ["release", [0, _turns(2)]]]

    result = _run(steps, live_run_id="r1")

    assert result["fetches"] == 2


def test_a_named_live_run_is_handed_to_the_chat():
    result = _run([["respond", _turns(live_run=R2)], ["changed", 2]])

    assert _types(result) == ["daiv:session-turns", "daiv:session-live-run"]
    assert result["events"][1]["detail"] == R2


def test_a_finished_run_is_not_followed_again():
    """The run's own end pokes while it still holds the slot, so the next fetch can still name it."""
    steps = [["respond", _turns(live_run=R1)], _ended("r1", "finished", frames=4), ["respond", _turns(3, live_run=R1)]]

    result = _run([*steps, ["changed", 3]], live_run_id="r1")

    assert result["fetches"] == 2
    assert result["handovers"] == []


@pytest.mark.parametrize(("live_run_id", "handovers"), [("r2", 0), ("", 1)])
def test_the_run_being_followed_is_not_handed_over_twice(live_run_id, handovers):
    steps = [["respond", _turns(2, live_run=R2)], ["changed", 2], ["respond", _turns(3, live_run=R2)], ["changed", 3]]

    result = _run(steps, live_run_id=live_run_id)

    assert result["fetches"] == 2
    assert len(result["handovers"]) == handovers


def test_after_its_own_turn_the_page_takes_only_the_header():
    """The tab that sent the turn keeps what it streamed; the header and server-only segments still apply."""
    payload = _turns(2, run_count=3)

    result = _run([["respond", payload], _ended("r3", "finished", own=True, frames=12)])

    assert result["events"] == [{"type": "daiv:session-header", "detail": {**payload, "requestedAt": 0}}]


def test_a_new_run_after_its_own_turn_brings_the_transcript_back():
    steps = [
        ["respond", _turns(2, run_count=3)],
        _ended("r3", "finished", own=True, frames=12),
        ["respond", _turns(3, run_count=3)],
        ["changed", 3],
        ["respond", _turns(4, run_count=4)],
        ["changed", 4],
        ["respond", _turns(5, run_count=4)],
        ["changed", 5],
    ]

    result = _run(steps)

    assert _types(result) == ["daiv:session-header", "daiv:session-header", "daiv:session-turns", "daiv:session-turns"]


def test_a_dropped_stream_rejoins_after_two_seconds():
    steps = [["respond", _turns(2, live_run=R1)], _ended("r1", "connection_lost", frames=3), ["advance", 1999]]

    early = _run(steps, live_run_id="r1")
    result = _run([*steps, ["advance", 1]], live_run_id="r1")

    assert _types(early) == ["daiv:session-turns"]
    assert result["handovers"] == [{"id": "r1", "at": 2000}]
    assert result["events"][-1] == {"type": "daiv:session-live-run", "detail": R1}


def _failing(times: int, *, frames: int = 0) -> list:
    steps = []
    for attempt in range(times):
        steps += [["respond", _turns(attempt + 2, live_run=R1)], _ended("r1", "error", frames=frames)]
        steps.append(["advance", 60_000])
    return steps


def _delays(result: dict) -> list[int]:
    return [handover["at"] - ended for handover, ended in zip(result["handovers"], result["ends"], strict=True)]


def test_a_relay_that_keeps_failing_backs_off_to_thirty_seconds():
    result = _run(_failing(5), live_run_id="r1")

    assert _delays(result) == [2000, 4000, 8000, 16000, 30000]


def test_a_stream_that_delivered_frames_resets_the_back_off():
    steps = [*_failing(3), ["respond", _turns(9, live_run=R1)], _ended("r1", "stale", frames=1), ["advance", 60_000]]

    result = _run(steps, live_run_id="r1")

    assert _delays(result) == [2000, 4000, 8000, 2000]


def test_a_send_that_never_started_is_followed_at_once_if_the_server_runs_it():
    """The POST failed, yet the server may have recorded and started the run."""
    result = _run([["respond", _turns(2, live_run=R1, run_count=2)], _ended("r1", "not_started", own=True)])

    assert _types(result) == ["daiv:session-header", "daiv:session-live-run"]
    assert result["handovers"] == [{"id": "r1", "at": 0}]


def test_a_send_that_never_started_keeps_its_failed_turn_on_screen():
    steps = [["respond", _turns(2, run_count=2)], _ended("r1", "not_started", own=True)]

    result = _run([*steps, ["respond", _turns(3, run_count=2)], ["changed", 3]])

    assert _types(result) == ["daiv:session-header", "daiv:session-header"]


def test_a_run_named_while_a_send_was_queued_is_handed_over_again_once_the_send_ends():
    """The chat drops the first handover while its POST is in flight; the queued end is what frees the run."""
    steps = [["respond", _turns(2, live_run=R1)], ["changed", 2], ["respond", _turns(2, live_run=R1, run_count=2)]]

    result = _run([*steps, _ended("mine", "queued", own=True), ["advance", 60_000]])

    assert _types(result) == [
        "daiv:session-turns",
        "daiv:session-live-run",
        "daiv:session-turns",
        "daiv:session-live-run",
    ]
    assert result["handovers"] == [{"id": "r1", "at": 0}, {"id": "r1", "at": 0}]
    assert result["timers"] == 0


def test_a_queued_end_is_no_stream_failure():
    """It starts no back-off and marks nothing finished, so the run it names is followed at once."""
    result = _run([["respond", _turns(2, live_run=R1)], _ended("r1", "queued", own=True)], live_run_id="r1")

    assert result["handovers"] == [{"id": "r1", "at": 0}]
    assert _types(result) == ["daiv:session-turns", "daiv:session-live-run"]


def test_a_settled_session_never_reloads():
    result = _run([["respond", _turns(2, active=False)], ["changed", 2], ["advance", 60_000]])

    assert result["reloads"] == 0
    assert _types(result) == ["daiv:session-turns"]


def test_a_failed_fetch_schedules_nothing():
    """A failure only releases the page's held status; the next announcement fetches again."""
    result = _run([["fail", 500], ["changed", 2], ["advance", 600_000], ["fail", 0], ["changed", 2], ["advance", 1]])

    assert (result["fetches"], result["timers"]) == (2, 0)
    assert result["events"] == [{"type": "daiv:session-header", "detail": None}] * 2


def test_after_a_failed_fetch_an_unchanged_revision_still_fetches_and_rejoins_the_dropped_run():
    """A live run bumps no revision while it runs, so the reconnect resync is what brings the dropped stream back."""
    steps = [["fail", 502], _ended("r1", "connection_lost", frames=3), ["respond", _turns(1, live_run=R1)]]

    result = _run([*steps, ["changed", 1], ["advance", 2000]], live_run_id="r1")

    assert result["fetches"] == 2
    assert result["handovers"] == [{"id": "r1", "at": 2000}]
