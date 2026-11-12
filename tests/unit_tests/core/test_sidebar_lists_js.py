"""Behavioral tests for the `sidebarLists` Alpine store in ``core/js/sidebar-lists.js``.

Driven under node (stubbing Alpine, ``fetch``, timers, storage and the two list containers)
because the store's job is deciding when a fetched fragment may replace the rows already
shown, which no template assertion sees.
"""

from __future__ import annotations

import json

import pytest

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

SIDEBAR_LISTS_JS = DAIV_DIR / "core" / "static" / "core" / "js" / "sidebar-lists.js"

pytestmark = requires_node

LISTS = '<section data-testid="nav-sessions-group"><a data-row-key="s:a"></a><a data-row-key="s:b"></a></section>'
FRESH = '<section data-testid="nav-sessions-group"><a data-row-key="s:c"></a><a data-row-key="s:b"></a></section>'

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, stored, scroll, responses, actions, html } = JSON.parse(readFileSync(0, "utf8"));
const stores = {};
const local = { ...stored };
const session = scroll === null ? {} : { "daiv.sidebar.scroll": String(scroll) };
const fetched = [];
const timers = new Map();
const frames = [];
const windowListeners = {};
let nextTimer = 1;
let focused = null;
const results = [];

function makeContainer(name) {
  const container = { name, dataset: { url: "/dashboard/sidebar/lists/", viewing: "thread-1" }, rows: [] };
  const scroller = { scrollTop: 300 };
  Object.defineProperty(container, "innerHTML", {
    get() { return this._html; },
    set(value) {
      this._html = value;
      scroller.scrollTop = 0;
      this.rows = [...value.matchAll(/data-row-key="([^"]+)"/g)].map(([, key]) => {
        const row = { dataset: { rowKey: key }, focus: () => { focused = `${name}:${key}`; } };
        row.closest = (selector) => {
          if (selector === "[data-row-key]") return row;
          return selector.includes("sidebar-lists") ? container : null;
        };
        return row;
      });
    },
  });
  container.querySelectorAll = () => container.rows;
  container.closest = (selector) => (selector === "[data-sidebar-scroll]" ? scroller : null);
  container.scroller = scroller;
  container.innerHTML = html;
  scroller.scrollTop = 300;
  return container;
}

const containers = [makeContainer("persistent"), makeContainer("sheet")];
const outside = { closest: () => null, focus: () => {} };

const sandbox = {
  console: { warn: () => {} },
  document: {
    activeElement: null,
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); },
    querySelector: () => containers[0],
    querySelectorAll: () => containers,
  },
  addEventListener: (name, cb) => { (windowListeners[name] ||= []).push(cb); },
  setTimeout: (cb) => { const id = nextTimer++; timers.set(id, cb); return id; },
  clearTimeout: (id) => { timers.delete(id); },
  requestAnimationFrame: (cb) => { frames.push(cb); },
  fetch: (url, init) => {
    fetched.push({ url: String(url), credentials: init?.credentials ?? null });
    const r = responses.shift() ?? { status: 200, body: "" };
    if (r.networkError) return Promise.reject(new TypeError("Failed to fetch"));
    return Promise.resolve({
      ok: r.status >= 200 && r.status < 300,
      redirected: Boolean(r.redirected),
      text: () => Promise.resolve(r.body),
    });
  },
  localStorage: {
    getItem: (k) => { if (stored === "blocked") throw new Error("SecurityError"); return local[k] ?? null; },
    setItem: (k, v) => { if (stored === "blocked") throw new Error("SecurityError"); local[k] = v; },
  },
  sessionStorage: {
    getItem: (k) => session[k] ?? null,
    setItem: (k, v) => { session[k] = v; },
  },
};
sandbox.window = sandbox;
sandbox.Alpine = { store: (name, value) => { stores[name] = value; value.init?.(); } };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const lists = stores.sidebarLists;
const flush = async () => {
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
  while (frames.length) frames.shift()();
};
const scroller = containers[0].scroller;
const rowOf = (key) => (key === "outside" ? outside : containers[0].rows.find((r) => r.dataset.rowKey === key));
for (const action of actions) {
  if (action === "refresh") lists.refresh();
  else if (action === "more") lists.more();
  else if (action === "runs-changed") (windowListeners["daiv:runs-changed"] || []).forEach((cb) => cb());
  else if (action === "tick") {
    const pending = [...timers.values()];
    timers.clear();
    pending.forEach((cb) => cb());
  } else if (action === "flush") await flush();
  else if (action.focus) sandbox.document.activeElement = rowOf(action.focus);
  else if (action.isOpen) results.push(lists.isOpen(...action.isOpen));
  else if (action.toggle) lists.toggle(...action.toggle);
  else if (action.restoreScroll) {
    lists.restoreScroll(scroller);
    results.push(scroller.scrollTop);
  } else if (action.saveScroll !== undefined) {
    scroller.scrollTop = action.saveScroll;
    lists.saveScroll(scroller);
  }
}

process.stdout.write(JSON.stringify({
  fetched,
  html: containers.map((c) => c.innerHTML),
  scrollTops: containers.map((c) => c.scroller.scrollTop),
  focused,
  results,
  limit: lists.limit,
  local,
  session,
}));
"""


def drive(actions, *, responses=(), stored=None, scroll=None, html=LISTS) -> dict:
    payload = {
        "src": str(SIDEBAR_LISTS_JS),
        "actions": list(actions),
        "responses": list(responses),
        "stored": stored if stored is not None else {},
        "scroll": scroll,
        "html": html,
    }
    return run_node(HARNESS, payload)


def ok(body=FRESH) -> dict:
    return {"status": 200, "body": body}


class TestExpandState:
    def test_the_server_default_holds_until_the_user_chooses(self):
        assert drive([{"isOpen": [5, True]}, {"isOpen": [5, False]}])["results"] == [True, False]

    def test_a_toggle_flips_and_is_remembered(self):
        state = drive([{"toggle": [5, True]}, {"isOpen": [5, True]}])

        assert state["results"] == [False]
        assert json.loads(state["local"]["daiv.sidebar.schedules"]) == {"5": False}

    def test_a_remembered_choice_beats_the_server_default(self):
        stored = {"daiv.sidebar.schedules": json.dumps({"5": True, "6": False})}

        assert drive([{"isOpen": [5, False]}, {"isOpen": [6, True]}], stored=stored)["results"] == [True, False]

    def test_unreadable_storage_falls_back_to_the_server_default(self):
        stored = {"daiv.sidebar.schedules": "not json"}

        assert drive([{"isOpen": [5, True]}], stored=stored)["results"] == [True]

    def test_blocked_storage_still_toggles_for_this_page(self):
        assert drive([{"toggle": [5, True]}, {"isOpen": [5, True]}], stored="blocked")["results"] == [False]


class TestRefresh:
    def test_runs_changing_refetches_the_lists_after_a_pause(self):
        waiting = drive(["runs-changed", "flush"])
        fired = drive(["runs-changed", "tick", "flush"], responses=[ok()])

        assert waiting["fetched"] == []
        assert fired["fetched"] == [
            {"url": "/dashboard/sidebar/lists/?limit=8&viewing=thread-1", "credentials": "same-origin"}
        ]

    def test_a_burst_of_refreshes_fetches_once(self):
        assert len(drive(["refresh", "refresh", "refresh", "tick", "flush"], responses=[ok()])["fetched"]) == 1

    def test_a_good_response_replaces_both_copies(self):
        assert drive(["refresh", "tick", "flush"], responses=[ok()])["html"] == [FRESH, FRESH]

    @pytest.mark.parametrize(
        "response",
        [
            {"status": 500, "body": FRESH},
            {"status": 200, "redirected": True, "body": "<form>Sign in</form>"},
            {"status": 200, "body": "<html><form>Sign in</form></html>"},
            {"networkError": True},
        ],
        ids=["server-error", "login-redirect", "not-the-lists", "offline"],
    )
    def test_a_failed_refresh_keeps_the_rows_shown(self, response):
        assert drive(["refresh", "tick", "flush"], responses=[response])["html"] == [LISTS, LISTS]

    def test_a_failed_refresh_does_not_stop_the_next_one(self):
        actions = ["refresh", "tick", "flush", "refresh", "tick", "flush"]
        state = drive(actions, responses=[{"networkError": True}, ok()])

        assert state["html"] == [FRESH, FRESH]

    def test_show_more_fetches_the_next_page_at_once(self):
        state = drive(["more", "flush"], responses=[ok()])

        assert state["limit"] == 16
        assert [entry["url"] for entry in state["fetched"]] == ["/dashboard/sidebar/lists/?limit=16&viewing=thread-1"]

    def test_a_refresh_keeps_the_show_more_depth(self):
        state = drive(["more", "flush", "refresh", "tick", "flush"], responses=[ok(), ok()])

        assert state["fetched"][-1]["url"] == "/dashboard/sidebar/lists/?limit=16&viewing=thread-1"

    def test_fetches_never_overlap(self):
        """A call made while one is in flight runs after it, with the depth asked for last."""
        state = drive(["more", "more", "flush"], responses=[ok(), ok()])

        assert [entry["url"] for entry in state["fetched"]] == [
            "/dashboard/sidebar/lists/?limit=16&viewing=thread-1",
            "/dashboard/sidebar/lists/?limit=24&viewing=thread-1",
        ]

    def test_focus_on_a_row_follows_it_into_the_fresh_rows(self):
        state = drive([{"focus": "s:b"}, "refresh", "tick", "flush"], responses=[ok()])

        assert state["focused"] == "persistent:s:b"

    def test_focus_outside_the_lists_is_left_alone(self):
        assert drive([{"focus": "outside"}, "refresh", "tick", "flush"], responses=[ok()])["focused"] is None

    def test_the_lists_keep_their_scroll_position(self):
        assert drive(["refresh", "tick", "flush"], responses=[ok()])["scrollTops"] == [300, 300]


class TestScroll:
    def test_restores_the_tabs_last_position(self):
        assert drive([{"restoreScroll": True}], scroll=240)["results"] == [240]

    def test_without_a_saved_position_nothing_moves(self):
        assert drive([{"restoreScroll": True}])["results"] == [300]

    def test_saves_the_position_for_this_tab(self):
        assert drive([{"saveScroll": 180}])["session"] == {"daiv.sidebar.scroll": "180"}
