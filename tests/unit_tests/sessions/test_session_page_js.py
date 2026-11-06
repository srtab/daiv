"""Drive ``session-page.js``'s real Alpine state under Node.

The component sits inside ``chat({…})``, whose ``streaming`` / ``resuming`` / ``waiting`` / ``archived`` flags it uses
through Alpine's merged scope; here they are plain properties on the component, which is how that scope resolves them.
"""

from __future__ import annotations

import json

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
  src, labelsSrc, initial, watch, delegations, repositories, thread, wide, storedTab, storedFolds, storageBlocked,
  archived, steps,
} = JSON.parse(readFileSync(0, "utf8"));
const SECTIONS = ["repositories", "environment", "context", "spend", "outputs"];
const components = {};
const fetches = [];
const pending = [];
const copied = [];
const storage = new Map([["key.tab", storedTab], ["key.folds", storedFolds]].filter(([, value]) => value != null));
let onMedia = null;
let clock = 10;
const json = { "session-header": { latest_run: initial, watch, run_count: 1, delegations, repositories } };
const watchers = {};
const frames = [];
const timers = [];
const intervals = [];
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
  setInterval: (cb, ms) => intervals.push({ cb, ms, cleared: false }),
  clearInterval: (id) => { if (intervals[id - 1]) intervals[id - 1].cleared = true; },
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
sandbox.navigator = { clipboard: { writeText: async (text) => { copied.push(text); } } };
sandbox.window = {
  location: { href: "https://daiv.test/sessions/t-1/" },
  matchMedia: () => ({ matches: wide, addEventListener: (_, cb) => { onMedia = cb; }, removeEventListener() {} }),
};
sandbox.URL = URL;
sandbox.Alpine = { data: (name, factory) => { components[name] = factory; } };
vm.createContext(sandbox);
vm.runInContext(readFileSync(labelsSrc, "utf8"), sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const labels = {
  running: "Running", queued: "Queued", watching: "Watching: {state}", watchOff: "Off for this merge request",
  watchFailed: "Couldn't change it",
};
const page = components.sessionPage({
  sessionUrl: "/session", ciWatchUrl: "/ci-watch", archiveUrl: "/archive", storageKey: "key", title: "Old",
  titleSuffix: "DAIV Sessions", labels,
});
const focused = [];
Object.assign(page, {
  streaming: false, resuming: false, waiting: false, archived, thread, csrfToken: "tok", $nextTick: (cb) => cb(),
  $watch: (expression, callback) => { watchers[expression] = callback; },
  $refs: {
    titleInput: { focus: () => focused.push("input"), select() {} },
    titleButton: { focus: () => focused.push("button"), getBoundingClientRect: () => ({ width: 312 }) },
  },
});
page.init();
const flush = () => new Promise((resolve) => setImmediate(resolve));
let lastRepositories;
const seen = [];
let sending = null;
for (const [action, value] of steps) {
  if (action === "set") Object.assign(page, value);
  else if (action === "poll") page.applyHeader(value);
  else if (action === "run-ended") page.settle();
  else if (action === "stream") { page.streaming = value; watchers.streaming?.(value); }
  else if (action === "provisional") sending = page.addProvisional(value);
  else if (action === "headings") seen.push((page.upNext?.groups ?? []).map((g, i) => page.upNextHeading(g, i)));
  else if (action === "reply") { pending.shift()(value.body, value.status); await sending; await flush(); }
  else if (action === "rename") page.startRename();
  else if (action === "draft") page.titleDraft = value;
  else if (action === "commit") sending = page.commitRename();
  else if (action === "cancel") page.cancelRename();
  else if (action === "ci-toggle-item") sending = page.toggleCiWatch(value);
  else if (action === "archive") sending = page.setArchived(value);
  else if (action === "tab") page.selectTab(value);
  else if (action === "fold") page.toggleFold(value);
  else if (action === "media") onMedia({ matches: value });
  else if (action === "open-details") page.openDetails(value);
  else if (action === "set-details") page.setDetails(value);
  else if (action === "copy") await page.copyText(value);
  else if (action === "copy-link") await page.copyLink(value);
  else if (action === "frame") { section.shown = value; frames.splice(0).forEach((cb) => cb()); }
  else if (action === "offers-retry") seen.push(value.map((marker) => page.canRetry(marker)));
  else if (action === "read") {
    seen.push({
      status: page.status, open: page.detailsOpen, runs: page.runCount,
      scrolled: scrolled.length, title: page.title, editing: page.editingTitle, titleWidth: page.titleWidth,
      docTitle: sandbox.document.title, archived: page.archived, ci: page.ci,
      notes: page.ci.map((item) => page.ciNote(item)), ciFor: page.repositoryRows.map((row) => page.ciFor(row)),
      cardNotes: page.repositoryRows.map((row) => page.ciNote(page.ciFor(row))),
      tab: page.detailsTab, folds: Object.fromEntries(SECTIONS.map((s) => [s, page.isFolded(s)])),
      summary: page.summary, repositoriesSummary: page.repositoriesSummary,
      explanations: page.ci.map((item) => page.ciExplanation(item)), upNext: page.upNext,
      ciTexts: page.ci.map((item) => page.ciText(item)),
      delegations: page.delegations, legs: (page.delegations?.legs ?? []).map((leg) => leg.repo_id),
      repositories: page.repositories, repositoryRows: page.repositoryRows,
      repositoriesKept: page.repositories === lastRepositories,
      timers: timers.map((timer) => timer.ms),
      intervals: intervals.filter((i) => !i.cleared).map((i) => i.ms),
    });
    lastRepositories = page.repositories;
  }
}
process.stdout.write(JSON.stringify({
  seen, fetches: fetches.length, requests: fetches, focused, copied,
  stored_tab: storage.get("key.tab") ?? null, stored_folds: storage.get("key.folds") ?? null,
}));
"""

DONE = {"label": "Done", "variant": "success"}
FAILED = {"label": "Failed", "variant": "failed"}
RUNNING = {"variant": "running", "label": "Running"}
WATCHING = {"state": "running", "label": "Waiting for CI", "tone": "attn", "open": True}
WATCH_OFF = {"state": "failing", "label": "Failing", "tone": "fail", "open": False}
DEFAULT_FOLDS = {"repositories": False, "environment": True, "context": False, "spend": True, "outputs": True}


def _ci(status="running", *, explanation=None, iid=7) -> dict:
    return {
        "repo_id": "",
        "merge_request_iid": iid,
        "status": status,
        "label": status.capitalize(),
        "watch_enabled": True,
        "explanation_run_id": explanation,
    }


def _run(
    steps,
    *,
    initial=DONE,
    watch=None,
    delegations=None,
    repositories=None,
    thread=None,
    wide=True,
    stored_tab=None,
    stored_folds=None,
    storage_blocked=False,
    archived=False,
) -> dict:
    payload = {
        "src": str(SESSION_PAGE_JS),
        "labelsSrc": str(TRANSCRIPT_LABELS_JS),
        "initial": initial,
        "watch": watch,
        "delegations": delegations,
        "repositories": repositories,
        "thread": thread,
        "wide": wide,
        "storedTab": stored_tab,
        "storedFolds": stored_folds,
        "storageBlocked": storage_blocked,
        "archived": archived,
        "steps": steps,
    }
    return run_node(HARNESS, payload)


def _read(**kwargs) -> dict:
    return _run([["read", None]], **kwargs)["seen"][0]


def test_it_shows_the_server_rendered_latest_run():
    seen = _read(watch=WATCHING)

    assert (seen["status"], seen["runs"], seen["scrolled"]) == (DONE, 1, 0)


def test_a_session_without_runs_has_no_status():
    assert _read(initial=None)["status"] is None


def test_a_streamed_or_queued_run_overrides_the_status():
    steps = [["set", {"streaming": True}], ["read", None], ["set", {"streaming": False, "waiting": True}]]
    seen = _run([*steps, ["read", None]])["seen"]

    assert [s["status"] for s in seen] == [RUNNING, {"variant": "queued", "label": "Queued"}]


def test_each_poll_replaces_the_latest_run_and_the_ci():
    item = _ci()
    seen = _run([_with_ci([item]), ["poll", {"latest_run": FAILED, "watch": None, "ci": []}], ["read", None]])["seen"][
        0
    ]

    assert (seen["status"], seen["ci"]) == (FAILED, [])


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


def test_a_wide_page_always_shows_the_panel():
    assert _read(wide=True)["open"] is True


def test_the_sheet_on_a_narrow_page_starts_closed():
    assert _read(wide=False)["open"] is False


def test_the_column_cannot_be_closed_while_wide():
    seen = _run([["set-details", False], ["read", None]], wide=True)["seen"][0]

    assert seen["open"] is True


def test_narrowing_closes_the_sheet_and_widening_shows_the_column():
    steps = [["media", False], ["read", None], ["media", True], ["read", None]]

    assert [s["open"] for s in _run(steps, wide=True)["seen"]] == [False, True]


def test_the_overview_tab_and_default_folds_come_first():
    seen = _read()

    assert seen["tab"] == "overview"
    assert seen["folds"] == DEFAULT_FOLDS


def test_tab_and_folds_are_remembered():
    result = _run([["tab", "info"], ["fold", "spend"], ["read", None]])

    assert result["stored_tab"] == "info" and json.loads(result["stored_folds"])["spend"] is False


def test_a_reload_restores_the_tab_and_folds():
    seen = _read(stored_tab="info", stored_folds=json.dumps({"spend": False, "context": True}))

    assert (seen["tab"], seen["folds"]) == ("info", {**DEFAULT_FOLDS, "spend": False, "context": True})


def test_garbage_or_blocked_storage_falls_back_to_the_defaults():
    assert _read(stored_folds="{not json")["folds"] == DEFAULT_FOLDS
    assert _read(stored_tab="nonsense", stored_folds="[1]")["tab"] == "overview"
    assert _read(storage_blocked=True)["tab"] == "overview"


def test_blocked_storage_still_switches_tabs_and_folds():
    seen = _run([["tab", "info"], ["fold", "outputs"], ["read", None]], storage_blocked=True)["seen"][0]

    assert (seen["tab"], seen["folds"]["outputs"]) == ("info", False)


def test_a_poll_keeps_the_tab_and_folds():
    poll = ["poll", {"latest_run": DONE, "watch": None, "run_count": 2}]
    seen = _run([["tab", "info"], ["fold", "context"], poll, ["read", None]])["seen"][0]

    assert (seen["tab"], seen["folds"]["context"]) == ("info", True)


def test_opening_context_selects_overview_and_unfolds_it():
    steps = [["tab", "info"], ["fold", "context"], ["open-details", "context"], ["read", None]]
    result = _run(steps)

    assert (result["seen"][0]["tab"], result["seen"][0]["folds"]["context"]) == ("overview", False)
    assert (result["stored_tab"], json.loads(result["stored_folds"])["context"]) == ("overview", False)


def test_opening_delegations_selects_info():
    assert _run([["open-details", "delegations"], ["read", None]])["seen"][0]["tab"] == "info"


def test_polls_and_a_settled_run_update_the_run_count():
    result = _run([
        ["poll", {"latest_run": DONE, "watch": None, "run_count": 2}],
        ["read", None],
        ["run-ended", None],
        ["poll", {"latest_run": FAILED, "watch": None, "run_count": 3}],
        ["read", None],
    ])

    assert [s["runs"] for s in result["seen"]] == [2, 3]


def test_the_page_creates_no_timers():
    seen = _run([["stream", True], ["stream", False], ["stream", True], ["read", None]])["seen"][0]

    assert (seen["intervals"], seen["timers"]) == ([], [])


def test_opening_a_section_scrolls_once_the_panel_is_shown():
    steps = [["open-details", "context"], ["read", None], ["frame", False], ["read", None], ["frame", True]]
    seen = _run([*steps, ["read", None]], wide=False)["seen"]

    assert [(s["open"], s["scrolled"]) for s in seen] == [(True, 0), (True, 0), (True, 1)]


def test_copying_text_copies_it_as_is_and_a_link_whole():
    result = _run([["copy", "6f1c2a"], ["copy-link", "/sessions/6f1c2a/"]])

    assert result["copied"] == ["6f1c2a", "https://daiv.test/sessions/6f1c2a/"]


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


def test_a_served_title_updates_the_page():
    seen = _run([["poll", {"latest_run": DONE, "title": "Fix the lint"}], ["read", None]])["seen"][0]

    assert (seen["title"], seen["docTitle"]) == ("Fix the lint", "Fix the lint — DAIV Sessions")


def test_a_served_title_does_not_overwrite_a_rename_in_progress():
    steps = [["rename", None], ["draft", "Mine"], ["poll", {"latest_run": DONE, "title": "Fix the lint"}]]

    seen = _run([*steps, ["read", None]])["seen"][0]

    assert (seen["title"], seen["docTitle"], seen["editing"]) == ("Old", "Old — DAIV Sessions", True)


# ---------------------------------------------------------------------------
# Fix CI failures
# ---------------------------------------------------------------------------


def _with_ci(ci, watch=WATCHING) -> list:
    return ["poll", {"latest_run": DONE, "run_count": 1, "watch": watch, "ci": ci}]


def test_each_merge_requests_note_describes_its_watch():
    ci = [{**_ci(), "label": "Waiting for CI"}, {**_ci("failing", iid=8), "watch_enabled": False}]

    assert _run([_with_ci(ci), ["read", None]])["seen"][0]["notes"] == [
        "Watching: Waiting for CI",
        "Off for this merge request",
    ]


def test_unticking_turns_off_the_watch_on_the_merge_request_and_takes_the_response():
    item = _ci("failing")
    reply = {"ci": [{**item, "watch_enabled": False}], "watch": WATCH_OFF}
    result = _run([_with_ci([item]), ["ci-toggle-item", item], ["reply", {"body": reply}], ["read", None]])

    body = {"on": False, "merge_request_iid": 7}
    assert result["requests"][0] == {"url": "/ci-watch", "method": "POST", "body": body, "csrf": "tok"}
    seen = result["seen"][0]
    assert (seen["ci"], seen["notes"]) == (reply["ci"], ["Off for this merge request"])


def test_ticking_turns_the_watch_back_on():
    item = {**_ci(iid=5), "label": "Waiting for CI", "watch_enabled": False}
    reply = {"ci": [{**item, "watch_enabled": True}], "watch": WATCHING}
    steps = [_with_ci([item], WATCH_OFF), ["read", None], ["ci-toggle-item", item], ["reply", {"body": reply}]]
    result = _run([*steps, ["read", None]])

    assert result["requests"][0]["body"] == {"on": True, "merge_request_iid": 5}
    before, after = result["seen"]
    assert (before["notes"], after["notes"]) == (["Off for this merge request"], ["Watching: Waiting for CI"])


def test_a_merge_request_the_session_no_longer_owns_leaves_the_list_and_can_be_tried_again():
    item = _ci()
    refused = {"detail": "This session doesn't own that merge request", "reason": "not_owned", "ci": [], "watch": None}
    steps = [_with_ci([item]), ["ci-toggle-item", item], ["reply", {"status": 409, "body": refused}], ["read", None]]
    result = _run([*steps, ["ci-toggle-item", item]])

    seen = result["seen"][0]
    assert (seen["ci"], result["fetches"]) == ([], 2)


def test_a_repository_with_the_watch_off_disables_the_item_with_the_reason():
    item = {**_ci("failing"), "watch_enabled": False}
    refused = {"detail": "CI fixes are turned off for this repository", "reason": "disabled", "ci": [item]}
    result = _run([
        _with_ci([item], WATCH_OFF),
        ["ci-toggle-item", item],
        ["reply", {"status": 409, "body": {**refused, "watch": WATCH_OFF}}],
        ["ci-toggle-item", item],
        ["read", None],
    ])

    assert result["seen"][0]["notes"] == [refused["detail"]]
    assert result["fetches"] == 1


def test_toggling_ci_on_an_extra_repository_sends_its_repo():
    item = {**_ci("failing", iid=17), "repo_id": "g/mod", "watch_enabled": True}
    result = _run([_with_ci([item]), ["ci-toggle-item", item]])

    assert result["requests"][-1]["body"] == {"on": False, "merge_request_iid": 17, "repo_id": "g/mod"}


def test_a_disabled_watch_is_noted_on_its_own_merge_request_only():
    main = _ci("passing")
    extra = {**_ci("failing", iid=17), "repo_id": "g/mod", "watch_enabled": False}
    refused = {"detail": "CI watching is off for g/mod", "reason": "disabled", "ci": [main, extra], "watch": WATCHING}
    result = _run([
        _with_ci([main, extra]),
        ["ci-toggle-item", extra],
        ["reply", {"status": 409, "body": refused}],
        ["ci-toggle-item", extra],
        ["read", None],
        ["ci-toggle-item", main],
    ])

    assert result["seen"][0]["notes"] == ["Watching: Passing", "CI watching is off for g/mod"]
    assert [request["body"]["merge_request_iid"] for request in result["requests"]] == [17, 7]


def test_a_failed_toggle_is_noted_on_its_merge_request_and_cleared_by_the_next():
    main, extra = _ci("passing"), {**_ci("failing", iid=17), "repo_id": "g/mod"}
    steps = [_with_ci([main, extra]), ["ci-toggle-item", extra], ["reply", {"status": 502, "body": {}}], ["read", None]]
    retried = [["ci-toggle-item", extra], ["read", None]]
    first, retrying = _run([*steps, *retried])["seen"]

    assert first["notes"] == ["Watching: Passing", "Couldn't change it"]
    assert retrying["notes"] == ["Watching: Passing", "Watching: Failing"]


def test_a_failure_links_the_run_that_looked_at_it():
    ci = [_ci("failing", explanation="r-1"), _ci("failing", iid=8), _ci("passing", explanation="r-2", iid=9)]
    result = _run([["poll", {"latest_run": DONE, "ci": ci, "watch": WATCH_OFF}], ["read", None]])

    assert result["seen"][0]["explanations"] == ["#run-status-r-1", "", ""]


def test_a_failure_names_its_failing_jobs_after_its_label():
    ci = [
        _ci("failing") | {"label": "Stopped after 3 attempts", "failed_jobs": ["tests", "lint"]},
        _ci("failing", iid=8) | {"label": "Failing"},
        _ci("passing", iid=9) | {"label": "Passing", "failed_jobs": []},
    ]
    result = _run([["poll", {"latest_run": DONE, "ci": ci, "watch": WATCH_OFF}], ["read", None]])

    assert result["seen"][0]["ciTexts"] == ["Stopped after 3 attempts · tests, lint", "Failing", "Passing"]


@pytest.mark.parametrize(("status", "body"), [(502, {}), (409, {"detail": "Something else"}), (422, {"detail": []})])
def test_a_failed_toggle_keeps_the_ci_and_says_so(status, body):
    item = _ci()
    steps = [_with_ci([item]), ["ci-toggle-item", item], ["reply", {"status": status, "body": body}], ["read", None]]

    seen = _run(steps)["seen"][0]
    assert (seen["ci"], seen["notes"]) == ([item], ["Couldn't change it"])


def test_a_poll_without_ci_keeps_the_ci_the_page_has():
    item = _ci()
    result = _run([_with_ci([item]), ["poll", {"latest_run": FAILED}], ["read", None]])

    assert (result["seen"][0]["status"], result["seen"][0]["ci"]) == (FAILED, [item])


def test_a_poll_asked_for_before_the_toggle_answered_does_not_undo_it():
    result = _run(
        [
            ["ci-toggle-item", {**_ci(), "watch_enabled": False}],
            ["reply", {"body": {"ci": [_ci()], "watch": WATCHING}}],
            ["poll", {"latest_run": FAILED, "watch": None, "requestedAt": 9}],
            ["read", None],
            ["poll", {"latest_run": FAILED, "watch": None, "requestedAt": 11}],
            ["read", None],
        ],
        watch=None,
    )

    assert [(s["status"], s["ci"]) for s in result["seen"]] == [(FAILED, [_ci()]), (FAILED, [])]


# ---------------------------------------------------------------------------
# inbox: Up next
# ---------------------------------------------------------------------------


UP_NEXT = {
    "groups": [
        {
            "run_id": "r-2",
            "who": "",
            "is_viewer": True,
            "starting": False,
            "bubbles": [{"id": "inbox-e0", "kind": "chat_message", "text": "Fix the lint"}],
            "told": [],
        }
    ],
    "told": [],
}


def test_up_next_follows_the_header():
    result = _run([
        ["poll", {"latest_run": DONE, "up_next": UP_NEXT}],
        ["read", None],
        ["poll", {"latest_run": DONE, "up_next": None}],
        ["read", None],
    ])

    assert [seen["upNext"] for seen in result["seen"]] == [UP_NEXT, None]


def _groups(*groups) -> dict:
    return {
        "groups": [
            {"run_id": f"r-{i}", "bubbles": [], "told": [], "starting": False, **g} for i, g in enumerate(groups)
        ]
    }


def test_up_next_headings_say_whose_run_and_when():
    groups = _groups(
        {"is_viewer": True, "who": "Olga"}, {"is_viewer": False, "who": "Bruno"}, {"is_viewer": False, "who": ""}
    )
    result = _run([["poll", {"latest_run": DONE, "up_next": groups}], ["set", {"streaming": True}], ["headings", None]])

    assert result["seen"][0] == [
        "Next run · as you · starts when this one ends",
        "Then · as Bruno · after your run",
        "Then · after Bruno's run",
    ]


def test_a_promoted_run_reads_as_starting_and_an_idle_one_as_next():
    result = _run([
        ["poll", {"latest_run": DONE, "up_next": _groups({"is_viewer": False, "who": "Bruno", "starting": True})}],
        ["headings", None],
        ["poll", {"latest_run": DONE, "up_next": _groups({"is_viewer": False, "who": ""})}],
        ["headings", None],
    ])

    assert result["seen"] == [["Next run · as Bruno · starting"], ["Next run"]]


def _bubble_ids(up_next) -> list[list[str]]:
    return [[bubble["id"] for bubble in group["bubbles"]] for group in up_next["groups"]]


def test_a_provisional_message_joins_the_group_of_its_run():
    provisional = {"event_id": "e1", "run_id": "r-2", "text": "Also add a test"}
    result = _run([["poll", {"latest_run": DONE, "up_next": UP_NEXT}], ["provisional", provisional], ["read", None]])

    assert _bubble_ids(result["seen"][0]["upNext"]) == [["inbox-e0", "inbox-e1"]]
    assert result["fetches"] == 0


def test_a_provisional_message_with_no_run_waits_after_the_inbox_runs_before_the_jobs():
    up_next = {
        "groups": [
            {"run_id": "", "who": "Bruno", "is_viewer": False, "starting": False, "told": [],
             "bubbles": [{"id": "inbox-e0", "kind": "mention", "text": "@daiv look"}]},
            {"run_id": "r-9", "who": "", "is_viewer": False, "starting": False, "told": [],
             "bubbles": [{"id": "run-r-9", "kind": "job", "text": "Upgrade Django"}]},
        ],
        "told": [],
    }  # fmt: skip
    provisional = {"event_id": "e1", "run_id": "", "text": "Also add a test"}
    result = _run([["poll", {"latest_run": DONE, "up_next": up_next}], ["provisional", provisional], ["read", None]])

    assert _bubble_ids(result["seen"][0]["upNext"]) == [["inbox-e0"], ["inbox-e1"], ["run-r-9"]]


def test_a_provisional_message_shows_until_the_server_has_it():
    provisional = {"event_id": "e1", "run_id": "r-2", "text": "Also add a test"}
    with_it = {**UP_NEXT, "groups": [{**UP_NEXT["groups"][0], "bubbles": [{"id": "inbox-e1", "text": "x"}]}]}
    result = _run([
        ["provisional", provisional],
        ["read", None],
        ["poll", {"latest_run": DONE, "up_next": with_it, "requestedAt": 10}],
        ["read", None],
    ])

    first, then = (seen["upNext"] for seen in result["seen"])
    assert _bubble_ids(first) == [["inbox-e1"]]
    assert (first["groups"][0]["run_id"], first["groups"][0]["is_viewer"]) == ("r-2", True)
    assert _bubble_ids(then) == [["inbox-e1"]]


def test_a_reply_asked_for_before_the_message_was_sent_keeps_it():
    provisional = {"event_id": "e1", "run_id": "r-2", "text": "Also add a test"}
    result = _run([
        ["provisional", provisional],
        ["poll", {"latest_run": DONE, "up_next": None, "requestedAt": 5}],
        ["read", None],
        ["poll", {"latest_run": DONE, "up_next": None, "requestedAt": 11}],
        ["read", None],
    ])

    first, then = (seen["upNext"] for seen in result["seen"])
    assert _bubble_ids(first) == [["inbox-e1"]]
    assert then is None


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


def test_a_session_that_delegated_nothing_has_no_delegations():
    seen = _read()

    assert (seen["delegations"], seen["legs"]) == (None, [])


def test_the_popover_lists_the_legs_in_the_servers_order():
    legs = [_leg("a/two", "failed"), _leg("a/one", "running"), _leg("a/three", "finished")]

    seen = _read(delegations=_delegations(total=3, running=1, failed=1, legs=legs))

    assert seen["legs"] == ["a/two", "a/one", "a/three"]


def test_each_poll_replaces_the_delegations():
    polled = _delegations(failed=2)
    steps = [["poll", {"latest_run": DONE, "delegations": polled}], ["read", None]]

    seen = _run(steps, delegations=_delegations(running=2))["seen"][0]

    assert seen["delegations"] == polled


def test_a_poll_without_delegations_keeps_the_ones_the_page_has():
    delegations = _delegations(running=1)

    seen = _run([["poll", {"latest_run": FAILED}], ["read", None]], delegations=delegations)["seen"][0]

    assert seen["delegations"] == delegations


def _repo(repo_id, ref, mr_iid=None) -> dict:
    mr_url = f"https://gl/{repo_id}/-/merge_requests/{mr_iid}" if mr_iid else ""
    return {
        "repo_id": repo_id,
        "ref": ref,
        "mr_url": mr_url,
        "mr_iid": mr_iid,
        "mr_title": "",
        "mr_draft": False,
        "diff_stats": None,
        "ran": False,
    }


def test_the_repositories_card_follows_each_turns_payload():
    rendered = {"rows": [_repo("g/app", "daiv/fix", 7), _repo("g/mod", "main")], "publish_errors": []}
    published = {
        "rows": [_repo("g/app", "daiv/fix", 7), _repo("g/mod", "daiv/fix", 3)],
        "publish_errors": [{"repo_id": "g/lib", "error": "push rejected"}],
    }

    steps = [["read", None], ["poll", {"latest_run": DONE, "repositories": published}], ["read", None]]
    seen = _run(steps, repositories=rendered)["seen"]

    assert [s["repositories"] for s in seen] == [rendered, published]


def test_a_single_repository_session_has_no_repositories_card():
    assert _read()["repositories"] is None


def test_a_failed_fetch_keeps_the_repositories_card():
    repositories = {"rows": [_repo("g/app", "main"), _repo("g/mod", "main")], "publish_errors": []}

    seen = _run([["poll", None], ["read", None]], repositories=repositories)["seen"][0]

    assert seen["repositories"] == repositories


def test_the_main_row_takes_the_chats_branch_and_merge_request():
    repositories = {"rows": [_repo("g/app", "main", 7), _repo("g/mod", "main")], "publish_errors": []}
    mr = {"url": "https://gl/g/app/-/merge_requests/9", "id": 9, "title": "Fix the lint", "draft": True}

    seen = _read(repositories=repositories, thread={"ref": "daiv/fix", "merge_request": mr})

    main = {**_repo("g/app", "daiv/fix", 9), "mr_title": "Fix the lint", "mr_draft": True}
    assert seen["repositoryRows"] == [main, _repo("g/mod", "main")]


def test_without_the_chats_merge_request_the_main_row_keeps_the_servers():
    repositories = {"rows": [_repo("g/app", "main", 7), _repo("g/mod", "main")], "publish_errors": []}

    seen = _read(repositories=repositories, thread={"ref": "main", "merge_request": None})

    assert seen["repositoryRows"] == repositories["rows"]


def test_an_unchanged_repositories_card_is_left_alone():
    errors = [{"repo_id": "g/mod", "error": "x"}]
    repositories = {"rows": [_repo("g/app", "main"), _repo("g/mod", "main")], "publish_errors": errors}
    changed = {**repositories, "publish_errors": []}
    steps = [
        ["read", None],
        ["poll", {"latest_run": DONE, "repositories": repositories}],
        ["read", None],
        ["poll", {"latest_run": DONE, "repositories": changed}],
        ["read", None],
    ]

    kept = [s["repositoriesKept"] for s in _run(steps, repositories=repositories)["seen"]]

    assert kept == [False, True, False]


# ---------------------------------------------------------------------------
# repository cards: CI per merge request, the summary
# ---------------------------------------------------------------------------


THREE_ROWS = {"rows": [_repo("g/app", "main", 42), _repo("g/mod", "main", 17), _repo("g/common", "main")]}


def test_each_card_finds_its_own_merge_requests_ci():
    main, extra = _ci("passing", iid=42), {**_ci("failing", iid=17), "repo_id": "g/mod"}
    other_repo_same_iid = {**_ci("running", iid=42), "repo_id": "g/elsewhere"}
    seen = _run([_with_ci([other_repo_same_iid, extra, main]), ["read", None]], repositories=THREE_ROWS)["seen"][0]

    assert seen["ciFor"] == [main, extra, None]


def test_a_merge_request_whose_watch_never_registered_has_no_ci_and_no_note():
    main = _ci("passing", iid=42)
    seen = _run([_with_ci([main]), ["read", None]], repositories=THREE_ROWS)["seen"][0]

    assert (seen["ciFor"], seen["cardNotes"]) == ([main, None, None], ["Watching: Passing", "", ""])


def test_the_main_card_finds_the_ci_of_the_chats_live_merge_request():
    live = {"url": "https://gl/g/app/-/merge_requests/43", "id": 43, "title": "Fix", "draft": False}
    item = _ci("passing", iid=43)
    seen = _run([_with_ci([item]), ["read", None]], repositories=THREE_ROWS, thread={"merge_request": live})["seen"][0]

    assert seen["ciFor"][0] == item


def test_the_summary_counts_repositories_and_merge_requests():
    seen = _read(repositories=THREE_ROWS)

    assert seen["summary"] == {"main": "g/app", "more": 2, "mrs": 2, "mrIid": None, "tone": ""}


def test_the_summary_names_a_single_merge_request():
    repositories = {"rows": [_repo("g/app", "main", 42)]}

    assert _read(repositories=repositories)["summary"] == {
        "main": "g/app",
        "more": 0,
        "mrs": 1,
        "mrIid": 42,
        "tone": "",
    }


def test_a_page_without_repositories_has_an_empty_summary():
    assert _read()["summary"] == {"main": "", "more": 0, "mrs": 0, "mrIid": None, "tone": ""}


@pytest.mark.parametrize(
    ("tones", "worst"),
    [
        (["clear", "fail"], "fail"),
        (["attn", "found", "clear"], "found"),
        (["clear", "attn"], "attn"),
        (["clear"], "clear"),
    ],
)
def test_the_summary_ci_is_the_worst_tone(tones, worst):
    ci = [{**_ci(iid=index), "tone": tone} for index, tone in enumerate(tones)]
    seen = _run([_with_ci(ci), ["read", None]], repositories=THREE_ROWS)["seen"][0]

    assert seen["summary"]["tone"] == worst


@pytest.mark.parametrize(
    ("rows", "label"),
    [
        (THREE_ROWS["rows"], "3 · 2 MRs"),
        ([_repo("g/app", "main", 42), _repo("g/mod", "main")], "2 · 1 MR"),
        ([_repo("g/app", "main"), _repo("g/mod", "main")], "2"),
        ([_repo("g/app", "main", 42)], "1 MR"),
        ([_repo("g/app", "main")], ""),
    ],
)
def test_the_folded_repositories_section_counts_them_and_their_merge_requests(rows, label):
    assert _read(repositories={"rows": rows})["repositoriesSummary"] == label
