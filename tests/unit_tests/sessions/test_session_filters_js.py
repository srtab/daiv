"""Behavioral tests for the sessions filter bar (``sessions/js/session-filters.js``), run under node.

The module is evaluated in a `vm` context with just enough of the browser for it: the shared
``filter-dropdown.js`` it spreads, a recording ``htmx.ajax`` and ``swapResults``, and plain
objects standing in for the elements the component reads.
"""

from __future__ import annotations

import json

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

pytestmark = requires_node

JS_DIR = DAIV_DIR / "sessions" / "static" / "sessions" / "js"

HARNESS = """
import vm from "node:vm";
import fs from "node:fs";

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const calls = { ajax: [], swaps: [] };
const listeners = {};
const timers = new Map();
let nextTimer = 1;

const el = (props = {}) => {
  const classes = new Set(props.classes ?? []);
  return {
    id: props.id ?? null,
    dataset: { ...(props.dataset ?? {}) },
    hidden: props.hidden ?? false,
    classList: { contains: (c) => classes.has(c), add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, ...(props.rect ?? {}) }),
    querySelector: props.querySelector ?? (() => null),
    querySelectorAll: props.querySelectorAll ?? (() => []),
    focus() {},
  };
};

const elements = {};
const sandbox = {
  console,
  URLSearchParams,
  Promise,
  CSS: { escape: (s) => s },
  setTimeout: (fn) => { const id = nextTimer++; timers.set(id, fn); return id; },
  clearTimeout: (id) => timers.delete(id),
  htmx: {
    ajax: (method, url, opts) => {
      calls.ajax.push({ method, url, source: opts.source?.id ?? null, target: opts.target?.id ?? null });
      return Promise.resolve();
    },
  },
  location: { pathname: "/dashboard/sessions/", search: payload.search ?? "" },
  innerWidth: payload.innerWidth ?? 1048,
  addEventListener: () => {},
  dispatchEvent: () => {},
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
  createResultsSwap: () => ({
    swapResults: (url, opts) => calls.swaps.push({ url, push: opts?.push ?? true, quiet: opts?.quiet ?? false }),
  }),
  document: {
    addEventListener: (name, fn) => { listeners[name] = fn; },
    getElementById: (id) => elements[id] ?? null,
  },
};
sandbox.window = sandbox;
const registry = {};
sandbox.Alpine = { data: (name, factory) => { registry[name] = factory; } };
vm.createContext(sandbox);
vm.runInContext(payload.dropdown, sandbox);
vm.runInContext(payload.filters, sandbox);
listeners["alpine:init"]();

const runTimers = () => {
  const pending = [...timers.entries()];
  timers.clear();
  for (const [, fn] of pending) fn();
};

const bar = registry.filterBar();
bar.$root = el({ dataset: { scope: "mine", defaultSort: "recent" }, rect: { left: 0, width: 930 } });
bar.$refs = { filterMenu: el(), filterButton: el() };
bar.$nextTick = (fn) => fn();
const result = await (async () => { BODY })();
process.stdout.write(JSON.stringify({ result: result ?? null, calls }));
"""


def _run(body: str, **payload) -> dict:
    harness = HARNESS.replace("BODY", body)
    return run_node(
        harness,
        {
            "dropdown": (JS_DIR / "filter-dropdown.js").read_text(encoding="utf-8"),
            "filters": (JS_DIR / "session-filters.js").read_text(encoding="utf-8"),
            **payload,
        },
    )


def test_drilling_into_a_lazy_list_loads_it_as_its_own_request():
    out = _run("""
        const box = el({ id: "choices", dataset: { choicesUrl: "/dashboard/sessions/repo-choices/" } });
        bar.$root.querySelector = (selector) => (selector.includes("data-filter-choices") ? box : null);
        bar.scope = "all";
        bar.drill("repo");
    """)

    assert out["calls"]["ajax"] == [
        {
            "method": "GET",
            "url": "/dashboard/sessions/repo-choices/?scope=all",
            "source": "choices",
            "target": "choices",
        }
    ]


def test_drilling_reloads_only_the_choices_whose_params_changed():
    out = _run("""
        const box = (reloadOn) => el({ dataset: { choicesUrl: "/choices/", ...(reloadOn && { reloadOn }) } });
        const boxes = { repo: box("scope archived"), spend_model: box() };
        bar.$root.querySelector = (selector) => boxes[selector.match(/data-filter-choices="([^"]+)"/)?.[1]] ?? null;
        for (const archived of ["", "", "true"]) {
          bar.archived = archived;
          bar.drill("repo");
          bar.drill("spend_model");
        }
    """)

    assert [call["url"] for call in out["calls"]["ajax"]] == [
        "/choices/?scope=mine",
        "/choices/?scope=mine",
        "/choices/?scope=mine&archived=true",
    ]


def test_picking_another_repository_drops_the_merge_request():
    out = _run("""
        Object.assign(bar, { scope: "all", repo: "a/one", mr: "12" });
        bar.pick("status", "FAILED");
        const kept = bar.mr;
        bar.pick("repo", "b/two");
        return kept;
    """)

    assert out["result"] == "12"
    assert out["calls"]["swaps"][-1]["url"] == "/dashboard/sessions/?scope=all&status=FAILED&repo=b%2Ftwo"


def test_drilling_clears_an_earlier_value_search():
    out = _run("""
        const items = [
          el({ dataset: { label: "a/one" }, hidden: true }),
          el({ dataset: { label: "b/two" }, hidden: false }),
        ];
        const panel = el({ querySelectorAll: () => items });
        bar.$root.querySelector = (selector) => (selector.includes("data-filter-panel") ? panel : null);
        bar.valueQuery = "two";
        bar.drill("repo");
        return items.map((item) => item.hidden);
    """)

    assert out["result"] == [False, False]


def test_a_live_refresh_waits_for_a_swap_in_flight_and_keeps_the_bars_state():
    out = _run(
        """
        const results = el({ classes: ["results--loading"] });
        elements["session-results"] = results;
        Object.assign(bar, { scope: "all", status: "FAILED", group: "status" });
        bar._refresh();
        runTimers();
        const whileLoading = calls.swaps.length;
        results.classList.remove("results--loading");
        runTimers();
        return whileLoading;
        """,
        search="?scope=all&group=status&page=2",
    )

    assert out["result"] == 0
    assert out["calls"]["swaps"] == [
        {"url": "/dashboard/sessions/?scope=all&status=FAILED&group=status&page=2", "push": False, "quiet": True}
    ]


def _display_left(inner_width: int, rect: dict) -> dict:
    out = _run(
        f"""
        const wrapper = el({{ rect: {json.dumps(rect)} }});
        bar.toggleDisplay(wrapper);
        return {{ open: bar.open, left: bar.displayLeft }};
        """,
        innerWidth=inner_width,
    )
    return out["result"]


def test_the_display_menu_stays_inside_a_phone_screen():
    placed = _display_left(375, {"left": 16, "width": 105})

    assert placed["open"] == "display"
    assert placed["left"] >= 0
    assert 16 + placed["left"] + 320 <= 375 - 16


def test_the_display_menu_lines_up_with_its_button_on_a_wide_screen():
    placed = _display_left(1048, {"left": 900, "width": 105})

    assert 900 + placed["left"] + 320 == 900 + 105
