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

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, initial, watch, wide, stored, storageBlocked, archived, steps } = JSON.parse(readFileSync(0, "utf8"));
const components = {};
const fetches = [];
const pending = [];
const storage = new Map(stored == null ? [] : [["key", stored]]);
let clock = 10;
const json = { "session-header": { latest_run: initial, watch, run_count: 1 } };
const frames = [];
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
sandbox.window = { matchMedia: () => ({ matches: wide, addEventListener() {}, removeEventListener() {} }) };
sandbox.Alpine = { data: (name, factory) => { components[name] = factory; } };
vm.createContext(sandbox);
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
      ciDisabled: Boolean(page.ciWatchDisabled), checked: page.ciWatchOn, ci: page.ci,
      explanations: page.ci.map((item) => page.ciExplanation(item)),
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
WATCHING = {"state": "running", "label": "Waiting for CI", "tone": "attn", "open": True}
WATCH_OFF = {"state": "failing", "label": "Failing", "tone": "fail", "open": False}


def _ci(status="running", *, explanation=None, iid=7) -> dict:
    return {"merge_request_iid": iid, "status": status, "watch_enabled": True, "explanation_run_id": explanation}


def _run(steps, *, initial=DONE, watch=None, wide=True, stored=None, storage_blocked=False, archived=False) -> dict:
    payload = {
        "src": str(SESSION_PAGE_JS),
        "initial": initial,
        "watch": watch,
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
    assert _read(watch=WATCH_OFF)["hint"] == "Off for this merge request"
    assert _read(watch=None)["hint"] == "Off for this merge request"


def test_unticking_turns_off_the_watch_on_the_merge_request_and_takes_the_response():
    reply = {"ci": [_ci("failing")], "watch": WATCH_OFF}
    result = _run([["ci-toggle", 7], ["reply", {"body": reply}], ["read", None]], watch=WATCHING)

    body = {"on": False, "merge_request_iid": 7}
    assert result["requests"][0] == {"url": "/ci-watch", "method": "POST", "body": body, "csrf": "tok"}
    seen = result["seen"][0]
    assert (seen["watch"], seen["checked"], seen["ci"]) == (WATCH_OFF, False, reply["ci"])


def test_ticking_turns_the_watch_back_on():
    reply = {"ci": [_ci()], "watch": WATCHING}
    result = _run([["read", None], ["ci-toggle", 5], ["reply", {"body": reply}], ["read", None]], watch=WATCH_OFF)

    assert result["requests"][0]["body"] == {"on": True, "merge_request_iid": 5}
    before, after = result["seen"]
    assert (before["checked"], after["checked"], after["hint"]) == (False, True, "Watching: Waiting for CI")


def test_a_merge_request_the_session_does_not_own_says_so():
    refused = {"detail": "This session doesn't own that merge request", "reason": "not_owned", "ci": [], "watch": None}
    result = _run([["ci-toggle", None], ["reply", {"status": 409, "body": refused}], ["read", None]], watch=WATCHING)

    seen = result["seen"][0]
    assert (seen["watch"], seen["ciDisabled"], seen["hint"]) == (None, False, refused["detail"])


def test_a_repository_with_the_watch_off_disables_the_item_with_the_reason():
    refused = {"detail": "CI fixes are turned off for this repository", "reason": "disabled", "ci": [], "watch": None}
    result = _run([
        ["ci-toggle", None],
        ["reply", {"status": 409, "body": refused}],
        ["ci-toggle", None],
        ["read", None],
    ])

    seen = result["seen"][0]
    assert (seen["watch"], seen["ciDisabled"], seen["hint"]) == (None, True, refused["detail"])
    assert result["fetches"] == 1


def test_a_failure_links_the_run_that_looked_at_it():
    ci = [_ci("failing", explanation="r-1"), _ci("failing", iid=8), _ci("passing", explanation="r-2", iid=9)]
    result = _run([["poll", {"latest_run": DONE, "ci": ci, "watch": WATCH_OFF}], ["read", None]])

    assert result["seen"][0]["explanations"] == ["#run-status-r-1", "", ""]


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
            ["reply", {"body": {"ci": [_ci()], "watch": WATCHING}}],
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
