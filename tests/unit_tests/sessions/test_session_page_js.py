"""Drive ``session-page.js``'s real Alpine state under Node.

The component sits inside ``chat({…})``, whose ``streaming`` / ``resuming`` / ``waiting`` flags it reads through
Alpine's merged scope; here they are plain properties on the component, which is how that scope resolves them.
"""

from __future__ import annotations

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

pytestmark = requires_node

SESSION_PAGE_JS = DAIV_DIR / "sessions/static/sessions/js/session-page.js"

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, initial, watch, wide, stored, storageBlocked, steps } = JSON.parse(readFileSync(0, "utf8"));
const components = {};
const fetches = [];
const pending = [];
const storage = new Map(stored == null ? [] : [["key", stored]]);
const json = { "session-latest-run": initial, "session-watch": watch };
const sandbox = {
  document: {
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); },
    getElementById: (id) => (id in json ? { textContent: JSON.stringify(json[id]) } : null),
  },
  localStorage: {
    getItem: (k) => { if (storageBlocked) throw new Error("blocked"); return storage.get(k) ?? null; },
    setItem: (k, v) => { if (storageBlocked) throw new Error("blocked"); storage.set(k, v); },
  },
  fetch: (url) => {
    fetches.push(url);
    return new Promise((resolve) => pending.push((body) => resolve({ ok: true, json: async () => body })));
  },
};
sandbox.window = { matchMedia: () => ({ matches: wide, addEventListener() {}, removeEventListener() {} }) };
sandbox.Alpine = { data: (name, factory) => { components[name] = factory; } };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const labels = { running: "Running", queued: "Queued" };
const page = components.sessionPage({ turnsUrl: "/turns", storageKey: "key", labels });
Object.assign(page, { streaming: false, resuming: false, waiting: false, $nextTick: (cb) => cb() });
page.init();
const settle = () => new Promise((resolve) => setImmediate(resolve));
const seen = [];
let refreshing = null;
for (const [action, value] of steps) {
  if (action === "set") Object.assign(page, value);
  else if (action === "poll") page.applyPoll(value);
  else if (action === "run-ended") refreshing = page.refresh();
  else if (action === "respond") { pending.shift()(value); await refreshing; await settle(); }
  else if (action === "toggle") page.toggleDetails();
  else if (action === "read") {
    seen.push({ status: page.status, retry: page.retryUrl, watch: page.watch, open: page.detailsOpen });
  }
}
process.stdout.write(JSON.stringify({ seen, fetches: fetches.length, stored: storage.get("key") ?? null }));
"""

DONE = {"id": "r1", "status": "SUCCESSFUL", "label": "Done", "variant": "success", "retry_url": "/runs/new/?from=r1"}
FAILED = {"id": "r2", "status": "FAILED", "label": "Failed", "variant": "failed", "retry_url": ""}
RUNNING = {"variant": "running", "label": "Running"}
WATCHING = {"state": "watching", "label": "Waiting for CI", "tone": "attn"}


def _run(steps, *, initial=DONE, watch=None, wide=True, stored=None, storage_blocked=False) -> dict:
    payload = {
        "src": str(SESSION_PAGE_JS),
        "initial": initial,
        "watch": watch,
        "wide": wide,
        "stored": stored,
        "storageBlocked": storage_blocked,
        "steps": steps,
    }
    return run_node(HARNESS, payload)


def _read(**kwargs) -> dict:
    return _run([["read", None]], **kwargs)["seen"][0]


def test_it_shows_the_server_rendered_latest_run_and_watch():
    assert _read(watch=WATCHING) | {"open": None} == {
        "status": DONE,
        "retry": "/runs/new/?from=r1",
        "watch": WATCHING,
        "open": None,
    }


def test_a_session_without_runs_has_no_status():
    assert _read(initial=None)["status"] is None


def test_a_streamed_or_queued_run_overrides_the_status_and_hides_retry():
    steps = [["set", {"streaming": True}], ["read", None], ["set", {"streaming": False, "waiting": True}]]
    seen = _run([*steps, ["read", None]])["seen"]

    assert [(s["status"], s["retry"]) for s in seen] == [(RUNNING, ""), ({"variant": "queued", "label": "Queued"}, "")]


def test_each_poll_replaces_the_latest_run_and_the_watch():
    seen = _run([["poll", {"latest_run": FAILED, "watch": WATCHING}], ["read", None]], watch=None)["seen"][0]

    assert (seen["status"], seen["watch"]) == (FAILED, WATCHING)


def test_a_run_ending_holds_running_until_its_settled_status_arrives():
    result = _run([
        ["set", {"streaming": True}],
        ["run-ended", None],
        ["set", {"streaming": False}],
        ["read", None],
        ["respond", {"latest_run": FAILED, "watch": None}],
        ["read", None],
    ])

    assert [s["status"] for s in result["seen"]] == [RUNNING, FAILED]
    assert result["fetches"] == 1


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
