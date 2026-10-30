"""Drive ``session-page.js``'s real Alpine state under Node.

The component sits inside ``chat({…})``, whose ``streaming`` / ``resuming`` / ``waiting`` / ``archived`` flags it uses
through Alpine's merged scope; here they are plain properties on the component, which is how that scope resolves them.
"""

from __future__ import annotations

import pytest

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

pytestmark = requires_node

SESSION_PAGE_JS = DAIV_DIR / "sessions/static/sessions/js/session-page.js"
TRANSCRIPT_LABELS_JS = DAIV_DIR / "chat/static/chat/js/transcript-labels.js"

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const {
  src, labelsSrc, initial, watch, delegations, wide, stored, storageBlocked, archived, steps,
} = JSON.parse(readFileSync(0, "utf8"));
const components = {};
const fetches = [];
const pending = [];
const storage = new Map(stored == null ? [] : [["key", stored]]);
let clock = 10;
const json = { "session-header": { latest_run: initial, watch, run_count: 1, delegations } };
const watchers = {};
const dispatched = [];
const frames = [];
const timers = [];
const scrolled = [];
const section = {
  shown: false,
  getClientRects() { return this.shown ? [{}] : []; },
  scrollIntoView() { scrolled.push("context"); },
};
const sandbox = {
  document: {
    hidden: false,
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); },
    getElementById: (id) => {
      if (id === "session-details-context") return section;
      return id in json ? { textContent: JSON.stringify(json[id]) } : null;
    },
  },
  requestAnimationFrame: (cb) => frames.push(cb),
  setTimeout: (cb, ms) => timers.push({ cb, ms }),
  clearTimeout: () => {},
  localStorage: {
    getItem: (k) => { if (storageBlocked) throw new Error("blocked"); return storage.get(k) ?? null; },
    setItem: (k, v) => { if (storageBlocked) throw new Error("blocked"); storage.set(k, v); },
  },
  performance: { now: () => clock },
  fetch: (url, options = {}) => {
    fetches.push({ url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null,
                   csrf: options.headers?.["X-CSRFToken"] ?? null });
    return new Promise((resolve) => pending.push((body, status = 200) =>
      resolve({ ok: status < 400, status, json: async () => body })));
  },
};
sandbox.document.title = "Old — DAIV Sessions";
sandbox.window = {
  matchMedia: () => ({ matches: wide, addEventListener() {}, removeEventListener() {} }),
  dispatchEvent: (event) => dispatched.push(event.type),
};
sandbox.CustomEvent = class { constructor(type) { this.type = type; } };
sandbox.Alpine = { data: (name, factory) => { components[name] = factory; } };
vm.createContext(sandbox);
vm.runInContext(readFileSync(labelsSrc, "utf8"), sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const labels = {
  running: "Running", queued: "Queued", watching: "Watching: {state}", watchOff: "Off for this merge request",
  watchFailed: "Couldn't change it",
};
const page = components.sessionPage({
  sessionUrl: "/session", ciWatchUrl: "/ci-watch", archiveUrl: "/archive", storageKey: "key",
  title: "Old", titleSuffix: "DAIV Sessions", labels,
});
const focused = [];
Object.assign(page, {
  streaming: false, resuming: false, waiting: false, archived, csrfToken: "tok", $nextTick: (cb) => cb(),
  $watch: (expression, callback) => { watchers[expression] = callback; },
  $refs: {
    titleInput: { focus: () => focused.push("input"), select() {} },
    titleButton: { focus: () => focused.push("button"), getBoundingClientRect: () => ({ width: 312 }) },
  },
});
page.init();
const flush = () => new Promise((resolve) => setImmediate(resolve));
const seen = [];
let sending = null;
for (const [action, value] of steps) {
  if (action === "set") Object.assign(page, value);
  else if (action === "poll") page.applyHeader(value);
  else if (action === "run-ended") page.settle();
  else if (action === "nav-running") watchers["$store.nav?.running"](...value);
  else if (action === "tick") timers.splice(0).forEach((timer) => timer.cb());
  else if (action === "reply") { pending.shift()(value.body, value.status); await sending; await flush(); }
  else if (action === "rename") page.startRename();
  else if (action === "draft") page.titleDraft = value;
  else if (action === "commit") sending = page.commitRename();
  else if (action === "cancel") page.cancelRename();
  else if (action === "ci-toggle") sending = page.toggleCiWatch(value ?? 7);
  else if (action === "archive") sending = page.setArchived(value);
  else if (action === "toggle") page.toggleDetails();
  else if (action === "open-details") page.openDetails("context");
  else if (action === "frame") { section.shown = value; frames.splice(0).forEach((cb) => cb()); }
  else if (action === "offers-retry") seen.push(value.map((marker) => page.canRetry(marker)));
  else if (action === "read") {
    seen.push({
      status: page.status, watch: page.watch, open: page.detailsOpen, runs: page.runCount,
      scrolled: scrolled.length, title: page.title, editing: page.editingTitle, titleWidth: page.titleWidth,
      docTitle: sandbox.document.title, archived: page.archived, hint: page.ciWatchHint,
      ciDisabled: Boolean(page.ciWatchDisabled), checked: page.ciWatchOn,
      delegations: page.delegations, delegationsLabel: page.delegationsBadge.label, dispatched: [...dispatched],
      delegationsState: page.delegationsBadge.state, legs: (page.delegations?.legs ?? []).map((leg) => leg.repo_id),
      timers: timers.map((timer) => timer.ms),
    });
  }
}
process.stdout.write(JSON.stringify({
  seen, fetches: fetches.length, requests: fetches, focused, stored: storage.get("key") ?? null,
}));
"""

DONE = {"label": "Done", "variant": "success"}
FAILED = {"label": "Failed", "variant": "failed"}
RUNNING = {"variant": "running", "label": "Running"}
WATCHING = {"state": "watching", "label": "Waiting for CI", "tone": "attn", "open": True}


def _run(
    steps, *, initial=DONE, watch=None, delegations=None, wide=True, stored=None, storage_blocked=False, archived=False
) -> dict:
    payload = {
        "src": str(SESSION_PAGE_JS),
        "labelsSrc": str(TRANSCRIPT_LABELS_JS),
        "initial": initial,
        "watch": watch,
        "delegations": delegations,
        "wide": wide,
        "stored": stored,
        "storageBlocked": storage_blocked,
        "archived": archived,
        "steps": steps,
    }
    return run_node(HARNESS, payload)


def _read(**kwargs) -> dict:
    return _run([["read", None]], **kwargs)["seen"][0]


def test_it_shows_the_server_rendered_latest_run_and_watch():
    seen = _read(watch=WATCHING)

    assert (seen["status"], seen["watch"], seen["runs"], seen["scrolled"]) == (DONE, WATCHING, 1, 0)


def test_a_session_without_runs_has_no_status():
    assert _read(initial=None)["status"] is None


def test_a_streamed_or_queued_run_overrides_the_status():
    steps = [["set", {"streaming": True}], ["read", None], ["set", {"streaming": False, "waiting": True}]]
    seen = _run([*steps, ["read", None]])["seen"]

    assert [s["status"] for s in seen] == [RUNNING, {"variant": "queued", "label": "Queued"}]


def test_each_poll_replaces_the_latest_run_and_the_watch():
    seen = _run([["poll", {"latest_run": FAILED, "watch": WATCHING}], ["read", None]], watch=None)["seen"][0]

    assert (seen["status"], seen["watch"]) == (FAILED, WATCHING)


def test_a_run_ending_holds_running_until_its_settled_status_arrives():
    result = _run([
        ["set", {"streaming": True}],
        ["run-ended", None],
        ["set", {"streaming": False}],
        ["read", None],
        ["poll", {"latest_run": FAILED, "watch": None}],
        ["read", None],
    ])

    assert [s["status"] for s in result["seen"]] == [RUNNING, FAILED]


def test_a_failed_settling_fetch_releases_the_held_status():
    seen = _run([["run-ended", None], ["read", None], ["poll", None], ["read", None]])["seen"]

    assert [s["status"] for s in seen] == [RUNNING, DONE]


def test_a_wide_page_opens_the_panel_as_last_left():
    assert _read(stored="open")["open"] is True
    assert _read(stored="closed")["open"] is False
    assert _read(stored=None)["open"] is False


def test_toggling_the_column_remembers_it():
    result = _run([["toggle", None], ["read", None]], stored=None)

    assert (result["seen"][0]["open"], result["stored"]) == (True, "open")


def test_the_sheet_on_a_narrow_page_starts_closed_and_is_not_remembered():
    result = _run([["read", None], ["toggle", None], ["read", None]], wide=False, stored="open")

    assert [s["open"] for s in result["seen"]] == [False, True]
    assert result["stored"] == "open"


def test_blocked_storage_starts_closed_and_still_toggles():
    result = _run([["read", None], ["toggle", None], ["read", None]], storage_blocked=True)

    assert [s["open"] for s in result["seen"]] == [False, True]


def test_polls_and_a_settled_run_update_the_run_count():
    result = _run([
        ["poll", {"latest_run": DONE, "watch": None, "run_count": 2}],
        ["read", None],
        ["run-ended", None],
        ["poll", {"latest_run": FAILED, "watch": None, "run_count": 3}],
        ["read", None],
    ])

    assert [s["runs"] for s in result["seen"]] == [2, 3]


def test_opening_a_section_scrolls_once_the_panel_is_shown():
    steps = [["open-details", None], ["read", None], ["frame", False], ["read", None], ["frame", True], ["read", None]]
    seen = _run(steps, wide=False)["seen"]

    assert [(s["open"], s["scrolled"]) for s in seen] == [(True, 0), (True, 0), (True, 1)]


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


def test_renaming_saves_the_trimmed_title_and_updates_the_page():
    result = _run([
        ["rename", None],
        ["read", None],
        ["draft", "  New name  "],
        ["commit", None],
        ["read", None],
        ["reply", {"body": {"title": "New name"}}],
        ["read", None],
    ])

    editing, saving, saved = result["seen"]
    assert (editing["editing"], editing["titleWidth"], saving["editing"]) == (True, 312, False)
    assert (saving["title"], saved["title"], saved["docTitle"]) == ("New name", "New name", "New name — DAIV Sessions")
    assert result["requests"] == [{"url": "/session", "method": "PATCH", "body": {"title": "New name"}, "csrf": "tok"}]
    assert result["focused"] == ["input"]


def test_enter_then_the_blur_it_causes_saves_once():
    result = _run([["rename", None], ["draft", "New"], ["commit", None], ["commit", None]])

    assert result["fetches"] == 1


@pytest.mark.parametrize("draft", ["", "   ", "Old"])
def test_a_blank_or_unchanged_title_keeps_the_old_one_without_saving(draft):
    result = _run([["rename", None], ["draft", draft], ["commit", None], ["read", None]])

    assert (result["seen"][0]["title"], result["fetches"]) == ("Old", 0)


def test_escape_cancels_and_returns_focus_to_the_title():
    result = _run([["rename", None], ["draft", "New"], ["cancel", None], ["commit", None], ["read", None]])

    assert (result["seen"][0]["title"], result["seen"][0]["editing"], result["fetches"]) == ("Old", False, 0)
    assert result["focused"] == ["input", "button"]


def test_a_refused_rename_puts_the_old_title_back():
    result = _run([
        ["rename", None],
        ["draft", "New"],
        ["commit", None],
        ["reply", {"status": 403, "body": {"detail": "no"}}],
        ["read", None],
    ])

    assert (result["seen"][0]["title"], result["seen"][0]["docTitle"]) == ("Old", "Old — DAIV Sessions")


# ---------------------------------------------------------------------------
# Fix CI failures
# ---------------------------------------------------------------------------


def test_the_ci_item_describes_the_watch():
    assert _read(watch=WATCHING)["hint"] == "Watching: Waiting for CI"
    assert _read(watch={"state": "green", "label": "Green", "tone": "clear", "open": False})["hint"] == "Green"
    assert _read(watch=None)["hint"] == "Off for this merge request"


def test_unticking_turns_off_the_watch_the_page_showed_and_takes_the_response():
    result = _run([["ci-toggle", 7], ["reply", {"body": None}], ["read", None]], watch=WATCHING)

    body = {"on": False, "merge_request_iid": 7, "seen_state": "watching"}
    assert result["requests"][0] == {"url": "/ci-watch", "method": "POST", "body": body, "csrf": "tok"}
    assert (result["seen"][0]["watch"], result["seen"][0]["checked"]) == (None, False)


def test_ticking_turns_the_watch_on():
    result = _run([["read", None], ["ci-toggle", 5], ["reply", {"body": WATCHING}], ["read", None]], watch=None)

    assert result["requests"][0]["body"] == {"on": True, "merge_request_iid": 5, "seen_state": "off"}
    before, after = result["seen"]
    assert (before["checked"], after["checked"], after["hint"]) == (False, True, "Watching: Waiting for CI")


@pytest.mark.parametrize("state", ["green", "exhausted", "unclear"])
def test_a_watch_that_is_over_shows_unticked_and_ticking_starts_a_new_one(state):
    ended = {"state": state, "label": state.title(), "tone": "fail", "open": False}
    result = _run([["read", None], ["ci-toggle", 7], ["reply", {"body": WATCHING}], ["read", None]], watch=ended)

    assert result["seen"][0]["checked"] is False
    assert result["requests"][0]["body"] == {"on": True, "merge_request_iid": 7, "seen_state": state}
    assert result["seen"][1]["watch"] == WATCHING


def test_a_lost_race_shows_the_watch_as_it_now_is():
    fixing = {"state": "fixing", "label": "Fixing", "tone": "attn", "open": True}
    result = _run(
        [
            ["ci-toggle", None],
            ["reply", {"status": 409, "body": {"reason": "changed", "watch": fixing}}],
            ["read", None],
        ],
        watch=WATCHING,
    )

    assert (result["seen"][0]["watch"], result["seen"][0]["ciDisabled"]) == (fixing, False)


def test_a_repository_with_the_watch_off_disables_the_item_with_the_reason():
    refused = {"detail": "CI fixes are turned off for this repository", "reason": "disabled", "watch": None}
    result = _run([
        ["ci-toggle", None],
        ["reply", {"status": 409, "body": refused}],
        ["ci-toggle", None],
        ["read", None],
    ])

    seen = result["seen"][0]
    assert (seen["watch"], seen["ciDisabled"], seen["hint"]) == (None, True, refused["detail"])
    assert result["fetches"] == 1


@pytest.mark.parametrize(("status", "body"), [(502, {}), (409, {"detail": "Something else"}), (422, {"detail": []})])
def test_a_failed_toggle_keeps_the_watch_and_says_so(status, body):
    result = _run([["ci-toggle", None], ["reply", {"status": status, "body": body}], ["read", None]], watch=WATCHING)

    seen = result["seen"][0]
    assert (seen["watch"], seen["hint"], seen["ciDisabled"]) == (WATCHING, "Couldn't change it", False)


def test_a_poll_without_a_watch_keeps_the_one_the_page_has():
    result = _run([["poll", {"latest_run": FAILED}], ["read", None]], watch=WATCHING)

    assert (result["seen"][0]["status"], result["seen"][0]["watch"]) == (FAILED, WATCHING)


def test_a_poll_asked_for_before_the_toggle_answered_does_not_undo_it():
    result = _run(
        [
            ["ci-toggle", None],
            ["reply", {"body": WATCHING}],
            ["poll", {"latest_run": FAILED, "watch": None, "requestedAt": 9}],
            ["read", None],
            ["poll", {"latest_run": FAILED, "watch": None, "requestedAt": 11}],
            ["read", None],
        ],
        watch=None,
    )

    assert [(s["status"], s["watch"]) for s in result["seen"]] == [(FAILED, WATCHING), (FAILED, None)]


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("archived", [True, False])
def test_archiving_and_unarchiving_post_the_choice(archived):
    result = _run(
        [["archive", archived], ["read", None], ["reply", {"body": {"archived": archived}}], ["read", None]],
        archived=not archived,
    )

    assert result["requests"][0] == {"url": "/archive", "method": "POST", "body": {"archived": archived}, "csrf": "tok"}
    assert [s["archived"] for s in result["seen"]] == [archived, archived]


def test_a_refused_archive_restores_the_state():
    result = _run([["archive", True], ["reply", {"status": 403, "body": {}}], ["read", None]])

    assert result["seen"][0]["archived"] is False


RETRYABLE = {"marker_id": "run-status-r2", "label": "Failed", "variant": "failed", "retry": "Please try again."}


def _marker(marker_id):
    return {"id": marker_id, "role": "run_status", "status": "failed"}


def test_retry_is_offered_on_the_latest_runs_marker_only():
    markers = [_marker("run-status-r2"), _marker("run-status-r1")]

    assert _run([["offers-retry", markers]], initial=RETRYABLE)["seen"] == [[True, False]]


def test_no_retry_when_the_server_offers_none():
    assert _run([["offers-retry", [_marker("run-status-r2")]]], initial={**RETRYABLE, "retry": ""})["seen"] == [[False]]


@pytest.mark.parametrize(
    "step", [["set", {"streaming": True}], ["set", {"waiting": True}], ["run-ended", None], ["set", {"archived": True}]]
)
def test_no_retry_while_a_run_is_busy_or_settling_or_the_session_is_archived(step):
    assert _run([step, ["offers-retry", [_marker("run-status-r2")]]], initial=RETRYABLE)["seen"] == [[False]]


# ---------------------------------------------------------------------------
# delegated jobs
# ---------------------------------------------------------------------------


def _leg(repo_id: str, state: str) -> dict:
    return {"repo_id": repo_id, "ref": "", "state": state, "merge_request_url": "", "session_url": ""}


def _delegations(*, total=2, running=0, waiting=0, failed=0, legs=None) -> dict:
    counts = {"total": total, "running": running, "waiting": waiting, "failed": failed}
    return {**counts, "batches": [], "legs": legs or []}


@pytest.mark.parametrize(
    ("counts", "label", "state"),
    [
        ({"running": 2, "waiting": 1, "failed": 1}, "2 delegated · 2 running", "running"),
        ({"waiting": 2, "failed": 1}, "2 delegated · 2 queued", "queued"),
        ({"failed": 1}, "2 delegated · 1 failed", "failed"),
        ({"total": 1}, "1 delegated", ""),
    ],
)
def test_the_top_bar_sums_up_the_delegated_jobs(counts, label, state):
    seen = _read(delegations=_delegations(**counts))

    assert (seen["delegationsLabel"], seen["delegationsState"]) == (label, state)


def test_a_session_that_delegated_nothing_has_no_delegations():
    seen = _read()

    assert (seen["delegations"], seen["delegationsLabel"], seen["delegationsState"], seen["legs"]) == (None, "", "", [])


def test_the_popover_lists_the_legs_in_the_servers_order():
    legs = [_leg("a/two", "failed"), _leg("a/one", "running"), _leg("a/three", "finished")]

    seen = _read(delegations=_delegations(total=3, running=1, failed=1, legs=legs))

    assert seen["legs"] == ["a/two", "a/one", "a/three"]


def test_each_poll_replaces_the_delegations():
    polled = _delegations(failed=2)
    steps = [["poll", {"latest_run": DONE, "delegations": polled}], ["read", None]]

    seen = _run(steps, delegations=_delegations(running=2))["seen"][0]

    assert seen["delegations"] == polled
    assert (seen["delegationsLabel"], seen["delegationsState"]) == ("2 delegated · 2 failed", "failed")


def test_a_poll_without_delegations_keeps_the_ones_the_page_has():
    delegations = _delegations(running=1)

    seen = _run([["poll", {"latest_run": FAILED}], ["read", None]], delegations=delegations)["seen"][0]

    assert (seen["delegations"], seen["delegationsLabel"]) == (delegations, "2 delegated · 1 running")


@pytest.mark.parametrize(
    ("counts", "checks"), [({"running": 1}, 1), ({"waiting": 1}, 1), ({"failed": 2}, 0), (None, 0)]
)
def test_a_run_ending_asks_for_a_check_while_a_delegated_job_is_unsettled(counts, checks):
    delegations = None if counts is None else _delegations(**counts)

    seen = _run([["nav-running", [2, 3]], ["tick", None], ["read", None]], delegations=delegations)["seen"][0]

    assert seen["dispatched"] == ["daiv:session-check"] * checks


def test_runs_ending_together_ask_for_one_check_after_the_interval():
    steps = [["nav-running", [2, 3]], ["nav-running", [1, 2]], ["read", None], ["tick", None], ["read", None]]

    before, after = _run(steps, delegations=_delegations(running=1))["seen"]

    assert (before["timers"], before["dispatched"]) == ([5000], [])
    assert after["dispatched"] == ["daiv:session-check"]


def test_a_run_starting_asks_for_no_check_of_its_own():
    seen = _run([["nav-running", [3, 2]], ["tick", None], ["read", None]], delegations=_delegations(running=1))

    assert seen["seen"][0]["dispatched"] == []


def test_a_check_due_after_the_jobs_settled_is_dropped():
    steps = [["nav-running", [2, 3]], ["poll", {"latest_run": DONE, "delegations": _delegations(failed=1)}]]

    seen = _run([*steps, ["tick", None], ["read", None]], delegations=_delegations(running=1))["seen"][0]

    assert seen["dispatched"] == []
