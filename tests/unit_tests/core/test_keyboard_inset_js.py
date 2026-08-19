"""Behavioral tests for ``core/js/keyboard-inset.js``.

Driven under node because the whole module is arithmetic over a viewport nothing in a
source-string assertion can move: the number it publishes has to compose with the pan
Safari performs on its own, and the two failure modes — paying for that pan twice, or not
at all — look identical in the source.

The loop-closing tests at the bottom keep the property name the module writes, the rule
that reads it and the page that loads the module from drifting apart: a rename on one side
alone leaves the composer anchored behind the keyboard again, silently.
"""

from __future__ import annotations

import re

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

KEYBOARD_INSET_JS = DAIV_DIR / "core" / "static" / "core" / "js" / "keyboard-inset.js"
INPUT_CSS = DAIV_DIR / "static_src" / "css" / "input.css"
BASE_HTML = DAIV_DIR / "accounts" / "templates" / "base.html"

pytestmark = requires_node

INNER_HEIGHT = 800

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, innerHeight, states, coalesce } = JSON.parse(readFileSync(0, "utf8"));

const listeners = { resize: [], scroll: [] };
const viewport = {
  height: innerHeight,
  offsetTop: 0,
  scale: 1,
  addEventListener: (name, cb) => listeners[name].push(cb),
  removeEventListener: () => {},
};

const published = [];
const frames = [];
const sandbox = {
  innerHeight,
  visualViewport: viewport,
  requestAnimationFrame: (cb) => frames.push(cb),
  document: { documentElement: { style: { setProperty: (name, value) => published.push([name, value]) } } },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const flush = () => frames.splice(0).forEach((cb) => cb());
flush();

for (const state of states) {
  Object.assign(viewport, state);
  listeners.resize.forEach((cb) => cb());
  listeners.scroll.forEach((cb) => cb());
  if (!coalesce) flush();
}
if (coalesce) flush();

process.stdout.write(JSON.stringify({ published }));
"""


def drive(*states: dict, coalesce: bool = False) -> list[str]:
    """Feed the module a sequence of visual-viewport geometries, return what it published.

    Each state is a patch on the viewport — ``height`` shrinks as a keyboard covers it,
    ``offsetTop`` is how far Safari has panned the page up on its own.
    """
    payload = {"src": str(KEYBOARD_INSET_JS), "innerHeight": INNER_HEIGHT, "states": list(states), "coalesce": coalesce}
    writes = run_node(HARNESS, payload)["published"]
    assert {name for name, _ in writes} == {"--keyboard-inset"}, writes
    return [value for _, value in writes]


def test_nothing_covering_the_page_publishes_a_zero_inset():
    """The first write happens before any event: the rule reading it is in the first paint,
    and a page loaded with a keyboard already up gets no resize to tell it so."""
    assert drive() == ["0px"]


def test_a_keyboard_the_page_has_not_moved_for_is_given_up_whole():
    """iOS resizes nothing for the keyboard, so the 340px it covers is 340px the scroller
    has to stop using."""
    assert drive({"height": INNER_HEIGHT - 340}) == ["0px", "340px"]


def test_a_pan_safari_already_did_is_not_paid_for_twice():
    """Safari lifts the focused field off the keyboard by panning the visual viewport, which
    moves the dock up by exactly the amount an inset would. Counting both is what leaves a
    band of dead space under the composer."""
    assert drive({"height": INNER_HEIGHT - 340, "offsetTop": 340}) == ["0px"]


def test_a_partial_pan_leaves_only_the_remainder():
    assert drive({"height": INNER_HEIGHT - 340, "offsetTop": 100}) == ["0px", "240px"]


def test_pinch_zoom_shrinks_the_visual_viewport_and_is_left_alone():
    """Zooming shrinks the visual viewport the same way a keyboard does. There the page
    bottom is off screen because the reader put it there, and reflowing under them is worse
    than the thing this module fixes."""
    assert drive({"height": 300, "offsetTop": 200, "scale": 2}) == ["0px"]


def test_dismissing_the_keyboard_hands_the_band_back():
    states = ({"height": INNER_HEIGHT - 340}, {"height": INNER_HEIGHT})
    assert drive(*states) == ["0px", "340px", "0px"]


def test_a_viewport_that_did_not_move_is_not_republished():
    """Every write resizes a scroll container, and the pan arrives as a stream of scroll
    events carrying the same geometry."""
    states = ({"height": INNER_HEIGHT - 340}, {"height": INNER_HEIGHT - 340})
    assert drive(*states) == ["0px", "340px"]


def test_a_burst_of_events_settles_into_one_write():
    states = ({"height": 700}, {"height": 600}, {"height": INNER_HEIGHT - 340})
    assert drive(*states, coalesce=True) == ["0px", "340px"]


PROPERTY = re.compile(r'setProperty\("(--[\w-]+)"')
CHAT_SCROLLER = re.compile(r"main:has\(\.chat-shell\)\s*\{([^}]*)\}")


def test_the_chat_scroller_gives_up_the_band_the_module_measures():
    """The publisher and its one consumer today. `margin-bottom`, not padding: the scroller
    has to be shorter, or the transcript tail stays under the keyboard with the dock."""
    published = PROPERTY.search(KEYBOARD_INSET_JS.read_text(encoding="utf-8"))
    assert published, "keyboard-inset.js no longer publishes a custom property"

    rule = CHAT_SCROLLER.search(INPUT_CSS.read_text(encoding="utf-8"))
    assert rule, "the chat scroller rule is gone — nothing gives up the keyboard's band"
    assert f"margin-bottom: var({published[1]}" in rule[1]


def test_the_shell_loads_it_on_every_page():
    """Shell-level like `surface-group.js`: the property has to exist wherever a surface
    might read it, and the chat page reaches it through this template, not its own block."""
    assert "core/js/keyboard-inset.js" in BASE_HTML.read_text(encoding="utf-8")
