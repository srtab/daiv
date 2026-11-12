"""Behavioral tests for the `sidebar` Alpine store in ``core/js/sidebar.js``.

Driven under node (stubbing Alpine, the DOM it queries and ``localStorage``) because the
store's job is deciding when a bare ``[`` belongs to it, which no template assertion sees.
"""

from __future__ import annotations

import re

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

SIDEBAR_JS = DAIV_DIR / "core" / "static" / "core" / "js" / "sidebar.js"


HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, initial, wide, legacy, storage, surfaces, actions, search, field, remembered, maxWidth } =
  JSON.parse(readFileSync(0, "utf8"));
const MIN_WIDTH = 272;
const stores = {};
const stored = { ...remembered };
const dataset = initial ? { sidebar: initial } : {};
// What base.html's head script applies from the remembered width before the store boots.
const vars = stored["daiv.sidebar.width"] ? { "--app-sidebar-chosen": `${stored["daiv.sidebar.width"]}px` } : {};
const listeners = {};
let assigned = null;
let fieldFocused = false;
let captured = false;
let resizingMidDrag = null;

// The CSS clamp the shell applies: `clamp(min, chosen, max)`.
const aside = {
  getBoundingClientRect: () => ({
    width: Math.max(MIN_WIDTH, Math.min(parseFloat(vars["--app-sidebar-chosen"]) || 0, maxWidth)),
  }),
};
const handle = {
  setPointerCapture: () => { captured = true; },
  addEventListener: (name, cb) => { (listeners[name] ||= new Set()).add(cb); },
  removeEventListener: (name, cb) => { listeners[name]?.delete(cb); },
};
const fire = (name, event) => [...(listeners[name] || [])].forEach((cb) => cb(event));
const bounds = { "--app-sidebar-min": `${MIN_WIDTH}px`, "--app-sidebar-max": `${maxWidth}px` };

// `legacy` drops `checkVisibility`, as Safari before 17.4 does.
const element = (s) => ({
  ...(legacy ? {} : { checkVisibility: () => s.visible }),
  getClientRects: () => (s.visible ? [{}] : []),
});

const sandbox = {
  document: {
    documentElement: {
      dataset,
      style: {
        setProperty: (name, value) => { vars[name] = value; },
        removeProperty: (name) => { delete vars[name]; },
      },
    },
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); },
    querySelector: (selector) => {
      if (!selector.includes("nav-search")) return element({ visible: wide });
      return search ? { href: "/dashboard/sessions/#search" } : null;
    },
    getElementById: (id) => {
      if (id === "app-sidebar") return aside;
      return id === "search" && field ? { ...element({ visible: true }), focus: () => { fieldFocused = true; } } : null;
    },
    querySelectorAll: () => surfaces.map(element),
  },
  localStorage: {
    setItem: (k, v) => { if (storage === "blocked") throw new Error("SecurityError"); stored[k] = v; },
    removeItem: (k) => { if (storage === "blocked") throw new Error("SecurityError"); delete stored[k]; },
  },
};
sandbox.getComputedStyle = () => ({ getPropertyValue: (name) => bounds[name] ?? "" });
sandbox.location = { assign: (url) => { assigned = url; } };
sandbox.window = sandbox;
sandbox.Alpine = { store: (name, value) => (stores[name] = value) };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const sidebar = stores.sidebar;
const seen = [];
for (const action of actions) {
  let prevented = false;
  const preventDefault = () => { prevented = true; };
  if (action.click) sidebar.toggle();
  else if (action.reset) sidebar.resetWidth();
  else if (action.drag) {
    const button = action.button ?? 0;
    sidebar.resizeStart({ button, pointerId: 1, clientX: 1000, currentTarget: handle, preventDefault });
    for (const dx of action.drag) fire("pointermove", { clientX: 1000 + dx });
    resizingMidDrag = "sidebarResizing" in dataset;
    fire(action.cancel ? "pointercancel" : "pointerup", {});
    seen.push(prevented);
  } else if (action.resize) {
    sidebar.resizeKey({ key: action.resize, preventDefault });
    seen.push(prevented);
  } else {
    const typing = action.typing;
    const target = {
      isContentEditable: typing === "contenteditable",
      closest: () => (typing && typing !== "contenteditable" ? {} : null),
    };
    const { altGraph, ...key } = action.key;
    const getModifierState = (name) => name === "AltGraph" && Boolean(altGraph);
    sidebar.onKey({ key: "[", ...key, target, preventDefault, getModifierState });
    seen.push(prevented);
  }
}
process.stdout.write(JSON.stringify({
  rail: sidebar.rail, attr: dataset.sidebar ?? null, stored, prevented: seen, assigned, fieldFocused,
  width: sidebar.width, minWidth: sidebar.minWidth, maxWidth: sidebar.maxWidth,
  chosen: vars["--app-sidebar-chosen"] ?? null, resizing: "sidebarResizing" in dataset, resizingMidDrag, captured,
  listening: Object.values(listeners).reduce((n, set) => n + set.size, 0),
}));
"""


def drive(
    actions,
    *,
    initial=None,
    wide=True,
    legacy=False,
    storage="ok",
    surfaces=(),
    search=True,
    field=False,
    remembered=None,
    max_width=512,
):
    """``wide`` is whether the toggle button is on screen, which is what the shortcut follows. ``search`` is
    whether the sidebar has its Search row, and ``field`` whether the page has the sessions search box.
    The sidebar shows 272px (17rem) clamped up to ``max_width``, or the width ``remembered`` holds."""
    payload = {**locals(), "src": str(SIDEBAR_JS), "surfaces": list(surfaces), "remembered": remembered or {}}
    return run_node(HARNESS, {**payload, "maxWidth": max_width})


@requires_node
def test_starts_from_the_attribute_the_head_script_set():
    assert drive([], initial="rail")["rail"] is True
    assert drive([])["rail"] is False


@requires_node
def test_the_toggle_flips_the_attribute_and_remembers_it():
    collapsed = drive([{"click": True}])
    expanded = drive([{"click": True}, {"click": True}])

    assert (collapsed["attr"], collapsed["stored"]) == ("rail", {"daiv.sidebar": "rail"})
    assert (expanded["attr"], expanded["stored"]) == (None, {})


@requires_node
def test_blocked_storage_still_toggles_for_this_page():
    state = drive([{"click": True}], storage="blocked")
    assert (state["rail"], state["attr"], state["stored"]) == (True, "rail", {})


@requires_node
def test_a_bare_bracket_toggles_and_claims_the_key():
    state = drive([{"key": {}}, {"key": {}}, {"key": {}}])
    assert state["rail"] is True
    assert state["prevented"] == [True, True, True]


@requires_node
def test_other_keys_are_left_alone():
    assert drive([{"key": {"key": "]"}}])["rail"] is False


@requires_node
def test_ctrl_or_meta_leaves_the_key_to_the_browser():
    for modifier in ("ctrlKey", "metaKey"):
        state = drive([{"key": {modifier: True}}])
        assert (state["rail"], state["prevented"]) == (False, [False]), modifier


@requires_node
def test_a_bracket_typed_with_altgr_or_option_still_toggles():
    """On pt, de, es or fr layouts `[` is AltGr+8 (Ctrl+Alt on Windows) or Option+8 on macOS."""
    assert drive([{"key": {"ctrlKey": True, "altKey": True, "altGraph": True}}])["rail"] is True
    assert drive([{"key": {"altKey": True}}])["rail"] is True


@requires_node
def test_a_held_key_toggles_once():
    assert drive([{"key": {}}, {"key": {"repeat": True}}, {"key": {"repeat": True}}])["rail"] is True


@requires_node
def test_works_where_check_visibility_is_missing():
    assert drive([{"key": {}}], legacy=True)["rail"] is True
    assert drive([{"key": {}}], legacy=True, surfaces=[{"visible": True}])["rail"] is False


@requires_node
def test_typing_a_bracket_in_a_field_types_it():
    for typing in ("textarea", "contenteditable"):
        state = drive([{"key": {}, "typing": typing}])
        assert (state["rail"], state["prevented"]) == (False, [False]), typing


@requires_node
def test_an_already_handled_key_is_left_alone():
    assert drive([{"key": {"defaultPrevented": True}}])["rail"] is False


@requires_node
def test_without_its_button_on_screen_there_is_nothing_to_collapse():
    state = drive([{"key": {}}], wide=False)
    assert (state["rail"], state["prevented"]) == (False, [False])


@requires_node
def test_an_open_dialog_sheet_or_menu_keeps_the_key():
    state = drive([{"key": {}}], surfaces=[{"visible": True}])
    assert (state["rail"], state["prevented"]) == (False, [False])


@requires_node
def test_hidden_surfaces_do_not_block_it():
    assert drive([{"key": {}}], surfaces=[{"visible": False}])["rail"] is True


def test_open_surfaces_are_found_by_the_shared_hook():
    """`data-surface` is the hook every floating surface carries (see input.css), so a new one
    blocks `[` without an edit here."""
    surfaces = re.search(r"const SURFACES = '([^']+)'", SIDEBAR_JS.read_text(encoding="utf-8"))
    assert surfaces and "[data-surface]" in surfaces.group(1).split(", ")


SLASH = {"key": {"key": "/"}}


@requires_node
def test_a_slash_opens_the_session_search():
    state = drive([SLASH])
    assert (state["assigned"], state["prevented"], state["rail"]) == ("/dashboard/sessions/#search", [True], False)


@requires_node
def test_on_the_sessions_page_a_slash_focuses_its_search_box():
    state = drive([SLASH], field=True)
    assert (state["fieldFocused"], state["assigned"], state["prevented"]) == (True, None, [True])


@requires_node
def test_a_slash_stands_down_like_the_bracket():
    for options in (
        {"actions": [{"key": {"key": "/"}, "typing": "textarea"}]},
        {"actions": [{"key": {"key": "/", "ctrlKey": True}}]},
        {"actions": [{"key": {"key": "/", "metaKey": True}}]},
        {"actions": [{"key": {"key": "/", "repeat": True}}]},
        {"actions": [SLASH], "surfaces": [{"visible": True}]},
    ):
        state = drive(**options)
        assert (state["assigned"], state["fieldFocused"], state["prevented"]) == (None, False, [False]), options


@requires_node
def test_a_slash_does_nothing_where_the_sidebar_has_no_search():
    state = drive([SLASH], search=False)
    assert (state["assigned"], state["prevented"]) == (None, [False])


WIDTH_KEY = "daiv.sidebar.width"


@requires_node
def test_dragging_the_edge_widens_the_sidebar_and_remembers_it():
    state = drive([{"drag": [40, 80]}])
    assert (state["width"], state["chosen"], state["stored"]) == (352, "352px", {WIDTH_KEY: "352"})


@requires_node
def test_a_drag_past_the_cap_remembers_the_width_shown():
    state = drive([{"drag": [300]}], max_width=400)
    assert (state["width"], state["chosen"], state["stored"]) == (400, "400px", {WIDTH_KEY: "400"})


@requires_node
def test_dragging_back_to_the_default_forgets_the_width():
    state = drive([{"drag": [-200]}], remembered={WIDTH_KEY: "352"})
    assert (state["width"], state["chosen"], state["stored"]) == (272, None, {})


@requires_node
def test_a_drag_marks_the_page_resizing_until_released():
    for cancel in (False, True):
        state = drive([{"drag": [16], "cancel": cancel}])
        assert (state["captured"], state["resizingMidDrag"], state["resizing"]) == (True, True, False), cancel
        assert state["listening"] == 0, cancel


@requires_node
def test_only_the_primary_button_drags():
    state = drive([{"drag": [80], "button": 2}])
    assert (state["chosen"], state["captured"], state["prevented"]) == (None, False, [False])


@requires_node
def test_reset_returns_to_the_default_and_forgets_the_width():
    state = drive([{"reset": True}], remembered={WIDTH_KEY: "400"})
    assert (state["width"], state["chosen"], state["stored"]) == (272, None, {})


@requires_node
def test_arrow_keys_nudge_the_width_by_a_step():
    state = drive([{"resize": "ArrowRight"}, {"resize": "ArrowRight"}, {"resize": "ArrowLeft"}])
    assert (state["width"], state["stored"], state["prevented"]) == (288, {WIDTH_KEY: "288"}, [True, True, True])


@requires_node
def test_home_and_end_jump_to_the_bounds():
    widest = drive([{"resize": "End"}])
    narrowest = drive([{"resize": "Home"}], remembered={WIDTH_KEY: "400"})

    assert (widest["width"], widest["stored"]) == (512, {WIDTH_KEY: "512"})
    assert (narrowest["width"], narrowest["stored"], narrowest["chosen"]) == (272, {}, None)


@requires_node
def test_other_keys_are_left_to_the_page():
    state = drive([{"resize": "Enter"}, {"resize": "ArrowUp"}])
    assert (state["chosen"], state["prevented"]) == (None, [False, False])


@requires_node
def test_reports_its_bounds_for_assistive_tech():
    state = drive([{"resize": "ArrowRight"}], max_width=448)
    assert (state["width"], state["minWidth"], state["maxWidth"]) == (288, 272, 448)


@requires_node
def test_blocked_storage_still_resizes_this_page():
    state = drive([{"drag": [48]}], storage="blocked")
    assert (state["width"], state["chosen"], state["stored"]) == (320, "320px", {})


BASE_TEMPLATE = DAIV_DIR / "core" / "templates" / "base.html"
HEAD_SCRIPT = re.compile(r"<head>.*?<script>(.*?)</script>", re.DOTALL)

HEAD_HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { script, stored, blocked } = JSON.parse(readFileSync(0, "utf8"));
const vars = {};
const dataset = {};
const sandbox = {
  document: { documentElement: { dataset, style: { setProperty: (name, value) => { vars[name] = value; } } } },
  localStorage: { getItem: (k) => { if (blocked) throw new Error("SecurityError"); return stored[k] ?? null; } },
};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);
process.stdout.write(JSON.stringify({ vars, sidebar: dataset.sidebar ?? null }));
"""


def head_script(stored, *, blocked=False):
    """Runs the inline ``<head>`` script that restores the sidebar before first paint."""
    script = HEAD_SCRIPT.search(BASE_TEMPLATE.read_text(encoding="utf-8"))
    assert script, "base.html's <head> has no inline script restoring the sidebar"
    return run_node(HEAD_HARNESS, {"script": script.group(1), "stored": stored, "blocked": blocked})


@requires_node
def test_the_head_script_ignores_a_width_that_is_not_a_number():
    assert head_script({WIDTH_KEY: "wide"})["vars"] == {}


@requires_node
def test_the_head_script_restores_what_the_store_remembered():
    remembered = drive([{"drag": [64]}, {"click": True}])
    restored = head_script(remembered["stored"])
    assert (restored["sidebar"], restored["vars"]) == ("rail", {"--app-sidebar-chosen": remembered["chosen"]})


@requires_node
def test_the_head_script_survives_blocked_storage():
    assert head_script({WIDTH_KEY: "400"}, blocked=True) == {"vars": {}, "sidebar": None}
