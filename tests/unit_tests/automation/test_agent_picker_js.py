"""Behavioral tests for the ``agentPicker`` component in ``automation/js/agent-picker.js``.

Driven under node because the effort state lives in the component: what the hidden input submits and what the pill
shows.
"""

from __future__ import annotations

import pytest

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

AGENT_PICKER_JS = DAIV_DIR / "automation" / "static" / "automation" / "js" / "agent-picker.js"

pytestmark = requires_node

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, args, actions } = JSON.parse(readFileSync(0, "utf8"));
const components = {};
const sandbox = {
  console: { warn: () => {}, error: () => {} },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
  dispatchEvent: () => true,
  surfaceGroup: { join: () => () => {} },
  document: { addEventListener: (name, cb) => { if (name === "alpine:init") cb(); } },
};
sandbox.window = sandbox;
sandbox.Alpine = { data: (name, factory) => (components[name] = factory) };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);

const picker = components.agentPicker(args);
picker.$watch = () => {};
picker.$nextTick = (cb) => cb();
picker.$refs = {};
picker.init();
for (const [method, ...params] of actions) picker[method](...params);

process.stdout.write(JSON.stringify({
  thinkingLevel: picker.thinkingLevel,
  agentModelValue: picker.agentModelValue,
  pillName: picker.pillLabel.name,
  effortDots: picker.pillLabel.effortDots,
}));
"""

SETTINGS_ARGS = {
    "providers": [{"slug": "anthropic", "label": "Anthropic"}],
    "catalogUrl": "/api/agent/models",
    "defaultAgentModel": "anthropic:claude-sonnet-4-6",
    "defaultThinkingLevel": "medium",
    "placeholderLabel": "Default (claude-sonnet-4-6)",
    "seedDefault": False,
    "required": False,
}
UNSELECTED_PILL_LABELS = pytest.mark.parametrize("placeholder_label", [SETTINGS_ARGS["placeholderLabel"], ""])
MEDIUM_DOTS = 3
XHIGH_DOTS = 5


def drive(actions: list[list[str]] | None = None, **overrides) -> dict:
    """Build the picker with the settings-page arguments, run ``init``, then call ``actions`` in order."""
    payload = {"src": str(AGENT_PICKER_JS), "args": {**SETTINGS_ARGS, **overrides}, "actions": actions or []}
    return run_node(HARNESS, payload)


def test_choosing_off_submits_off():
    state = drive([["selectEffort", "off"]])
    assert state["thinkingLevel"] == "off"


@UNSELECTED_PILL_LABELS
def test_an_unselected_pill_shows_no_effort_for_off(placeholder_label):
    """Both unselected names: the settings placeholder label and the derived "Default (…)" label."""
    state = drive([["selectEffort", "off"]], placeholderLabel=placeholder_label)
    assert state["agentModelValue"] == ""
    assert state["effortDots"] == 0


@UNSELECTED_PILL_LABELS
def test_an_unselected_pill_shows_the_picked_effort(placeholder_label):
    state = drive([["selectEffort", "xhigh"]], placeholderLabel=placeholder_label)
    assert state["agentModelValue"] == ""
    assert state["effortDots"] == XHIGH_DOTS


@UNSELECTED_PILL_LABELS
def test_an_unselected_pill_with_no_level_shows_the_default_effort(placeholder_label):
    state = drive(placeholderLabel=placeholder_label)
    assert state["thinkingLevel"] == ""
    assert state["effortDots"] == MEDIUM_DOTS


def test_a_selected_model_with_off_shows_no_effort():
    state = drive([["selectEffort", "off"]], initialAgentModel="anthropic:claude-opus-4-1")
    assert state["agentModelValue"] == "anthropic:claude-opus-4-1"
    assert state["effortDots"] == 0


def test_use_default_clears_off_back_to_the_default_effort():
    state = drive([["clearSelection"]], initialThinkingLevel="off")
    assert state["thinkingLevel"] == ""
    assert state["effortDots"] == MEDIUM_DOTS


def test_a_stored_off_seeds_the_picker_with_off():
    state = drive(initialThinkingLevel="off")
    assert state["thinkingLevel"] == "off"
    assert state["effortDots"] == 0
