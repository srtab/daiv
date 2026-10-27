"""Behavioral tests for the `sidebar` Alpine store in ``core/js/sidebar.js``.

Driven under node (stubbing Alpine, the DOM it queries and ``localStorage``) because the
store's job is deciding when a bare ``[`` belongs to it, which no template assertion sees.
"""

from __future__ import annotations

import re

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_picker_popovers import surface_rise_roster
from tests.unit_tests.test_template_comments import DAIV_DIR

SIDEBAR_JS = DAIV_DIR / "core" / "static" / "core" / "js" / "sidebar.js"


HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, initial, wide, legacy, storage, surfaces, actions } = JSON.parse(readFileSync(0, "utf8"));
const stores = {};
const stored = {};
const dataset = initial ? { sidebar: initial } : {};

// `legacy` drops `checkVisibility`, as Safari before 17.4 does.
const element = (s) => ({
  ...(legacy ? {} : { checkVisibility: () => s.visible }),
  getClientRects: () => (s.visible ? [{}] : []),
});

const sandbox = {
  document: {
    documentElement: { dataset },
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); },
    querySelector: () => element({ visible: wide }),
    querySelectorAll: () => surfaces.map(element),
  },
  localStorage: {
    setItem: (k, v) => { if (storage === "blocked") throw new Error("SecurityError"); stored[k] = v; },
    removeItem: (k) => { if (storage === "blocked") throw new Error("SecurityError"); delete stored[k]; },
  },
};
sandbox.Alpine = { store: (name, value) => (stores[name] = value) };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const sidebar = stores.sidebar;
const seen = [];
for (const action of actions) {
  if (action.click) sidebar.toggle();
  else {
    let prevented = false;
    const typing = action.typing;
    const target = {
      isContentEditable: typing === "contenteditable",
      closest: () => (typing && typing !== "contenteditable" ? {} : null),
    };
    const { altGraph, ...key } = action.key;
    const getModifierState = (name) => name === "AltGraph" && Boolean(altGraph);
    const preventDefault = () => { prevented = true; };
    sidebar.onKey({ key: "[", ...key, target, preventDefault, getModifierState });
    seen.push(prevented);
  }
}
process.stdout.write(JSON.stringify({ rail: sidebar.rail, attr: dataset.sidebar ?? null, stored, prevented: seen }));
"""


def drive(actions, *, initial=None, wide=True, legacy=False, storage="ok", surfaces=()):
    """``wide`` is whether the toggle button is on screen, which is what the shortcut follows."""
    return run_node(HARNESS, {**locals(), "src": str(SIDEBAR_JS), "surfaces": list(surfaces)})


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


def test_the_surface_list_covers_the_css_roster():
    """A surface enrolled in the `surface-rise` roster but missing here would let `[`
    collapse the sidebar under it."""
    listed = set(re.findall(r'"(\.[\w-]+)"', SIDEBAR_JS.read_text(encoding="utf-8")))
    assert surface_rise_roster() - listed == set()
