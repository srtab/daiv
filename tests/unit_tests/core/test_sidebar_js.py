"""Behavioral tests for the `sidebar` Alpine store in ``core/js/sidebar.js``.

Driven under node (stubbing Alpine, the DOM it queries and ``localStorage``) because the
store's job is deciding when a bare ``[`` belongs to it, which no template assertion sees.
"""

from __future__ import annotations

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

SIDEBAR_JS = DAIV_DIR / "core" / "static" / "core" / "js" / "sidebar.js"

pytestmark = requires_node

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, initial, wide, storage, surfaces, actions } = JSON.parse(readFileSync(0, "utf8"));
const stores = {};
const stored = {};
const dataset = initial ? { sidebar: initial } : {};

const sandbox = {
  document: {
    documentElement: { dataset },
    addEventListener: (name, cb) => { if (name === "alpine:init") cb(); },
    querySelectorAll: () => surfaces.map((s) => ({ checkVisibility: () => s.visible, position: s.position })),
  },
  getComputedStyle: (el) => ({ position: el.position }),
  localStorage: {
    setItem: (k, v) => { if (storage === "blocked") throw new Error("SecurityError"); stored[k] = v; },
  },
};
sandbox.window = sandbox;
sandbox.window.matchMedia = (query) => ({ matches: wide, query });
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
    const modifiers = { ctrlKey: false, metaKey: false, altKey: false, shiftKey: false };
    const preventDefault = () => { prevented = true; };
    sidebar.onKey({ key: "[", ...modifiers, defaultPrevented: false, ...action.key, target, preventDefault });
    seen.push(prevented);
  }
}
process.stdout.write(JSON.stringify({ rail: sidebar.rail, attr: dataset.sidebar ?? null, stored, prevented: seen }));
"""


def drive(actions, *, initial=None, wide=True, storage="ok", surfaces=()):
    payload = {
        "src": str(SIDEBAR_JS),
        "initial": initial,
        "wide": wide,
        "storage": storage,
        "surfaces": list(surfaces),
        "actions": actions,
    }
    return run_node(HARNESS, payload)


def test_starts_from_the_attribute_the_head_script_set():
    assert drive([], initial="rail")["rail"] is True
    assert drive([])["rail"] is False


def test_the_toggle_flips_the_attribute_and_remembers_it():
    collapsed = drive([{"click": True}])
    expanded = drive([{"click": True}], initial="rail")

    assert (collapsed["attr"], collapsed["stored"]) == ("rail", {"daiv.sidebar": "rail"})
    assert (expanded["attr"], expanded["stored"]) == (None, {"daiv.sidebar": "full"})


def test_blocked_storage_still_toggles_for_this_page():
    state = drive([{"click": True}], storage="blocked")
    assert (state["rail"], state["attr"], state["stored"]) == (True, "rail", {})


def test_a_bare_bracket_toggles_and_claims_the_key():
    state = drive([{"key": {}}, {"key": {}}, {"key": {}}])
    assert state["rail"] is True
    assert state["prevented"] == [True, True, True]


def test_other_keys_are_left_alone():
    assert drive([{"key": {"key": "]"}}])["rail"] is False


def test_a_modifier_leaves_the_key_to_the_browser():
    for modifier in ("ctrlKey", "metaKey", "altKey", "shiftKey"):
        state = drive([{"key": {modifier: True}}])
        assert (state["rail"], state["prevented"]) == (False, [False]), modifier


def test_typing_a_bracket_in_a_field_types_it():
    for typing in ("textarea", "contenteditable"):
        state = drive([{"key": {}, "typing": typing}])
        assert (state["rail"], state["prevented"]) == (False, [False]), typing


def test_an_already_handled_key_is_left_alone():
    assert drive([{"key": {"defaultPrevented": True}}])["rail"] is False


def test_below_lg_there_is_nothing_to_collapse():
    state = drive([{"key": {}}], wide=False)
    assert (state["rail"], state["prevented"]) == (False, [False])


def test_an_open_dialog_sheet_or_menu_keeps_the_key():
    state = drive([{"key": {}}], surfaces=[{"visible": True, "position": "absolute"}])
    assert (state["rail"], state["prevented"]) == (False, [False])


def test_hidden_surfaces_and_the_details_column_do_not_block_it():
    """The session details panel is a static column from 1100px, not something open over the page."""
    surfaces = [{"visible": False, "position": "fixed"}, {"visible": True, "position": "static"}]
    assert drive([{"key": {}}], surfaces=surfaces)["rail"] is True
