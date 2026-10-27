"""Drive ``session-header.js``'s real Alpine state under Node.

The header sits inside ``chat({…})``, whose ``streaming`` / ``resuming`` / ``waiting`` flags it reads through Alpine's
merged scope; here they are plain properties on the component, which is how that scope resolves them.
"""

from __future__ import annotations

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

pytestmark = requires_node

SESSION_HEADER_JS = DAIV_DIR / "sessions/static/sessions/js/session-header.js"

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, initial, steps } = JSON.parse(readFileSync(0, "utf8"));
const components = {};
const fetches = [];
const pending = [];
const sandbox = {
  document: {
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); },
    getElementById: (id) => (id === "session-header-latest-run" ? { textContent: JSON.stringify(initial) } : null),
  },
  fetch: (url) => {
    fetches.push(url);
    return new Promise((resolve) => pending.push((body) => resolve({ ok: true, json: async () => body })));
  },
};
sandbox.Alpine = { data: (name, factory) => { components[name] = factory; } };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const header = components.sessionHeader({ turnsUrl: "/turns", labels: { running: "Running", queued: "Queued" } });
Object.assign(header, { streaming: false, resuming: false, waiting: false, $el: { closest: () => null } });
header.init();
const settle = () => new Promise((resolve) => setImmediate(resolve));
const seen = [];
let refreshing = null;
for (const [action, value] of steps) {
  if (action === "set") Object.assign(header, value);
  else if (action === "poll") header.applyLatest(value);
  else if (action === "run-ended") refreshing = header.refresh();
  else if (action === "respond") { pending.shift()(value); await refreshing; await settle(); }
  else if (action === "read") seen.push({ status: header.status, retry: header.retryUrl });
}
process.stdout.write(JSON.stringify({ seen, fetches: fetches.length }));
"""

DONE = {"id": "r1", "status": "SUCCESSFUL", "label": "Done", "variant": "success", "retry_url": "/runs/new/?from=r1"}
FAILED = {"id": "r2", "status": "FAILED", "label": "Failed", "variant": "failed", "retry_url": ""}
RUNNING = {"variant": "running", "label": "Running"}


def _run(steps: list, initial: dict | None = DONE) -> dict:
    return run_node(HARNESS, {"src": str(SESSION_HEADER_JS), "initial": initial, "steps": steps})


def test_it_shows_the_server_rendered_latest_run():
    assert _run([["read", None]])["seen"] == [{"status": DONE, "retry": "/runs/new/?from=r1"}]


def test_a_session_without_runs_has_no_status():
    assert _run([["read", None]], initial=None)["seen"] == [{"status": None, "retry": ""}]


def test_a_streamed_run_reads_as_running_without_retry():
    steps = [["set", {"streaming": True}], ["read", None], ["set", {"streaming": False, "waiting": True}]]
    steps.append(["read", None])
    seen = _run(steps)["seen"]

    assert seen == [{"status": RUNNING, "retry": ""}, {"status": {"variant": "queued", "label": "Queued"}, "retry": ""}]


def test_each_poll_replaces_the_latest_run():
    assert _run([["poll", FAILED], ["read", None]])["seen"] == [{"status": FAILED, "retry": ""}]


def test_a_run_ending_holds_running_until_its_settled_status_arrives():
    result = _run([
        ["set", {"streaming": True}],
        ["run-ended", None],
        ["set", {"streaming": False}],
        ["read", None],
        ["respond", {"latest_run": FAILED}],
        ["read", None],
    ])

    assert result["seen"] == [{"status": RUNNING, "retry": ""}, {"status": FAILED, "retry": ""}]
    assert result["fetches"] == 1
