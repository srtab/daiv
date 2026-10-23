"""Drive the shared repository picker's real Alpine state under Node."""

from __future__ import annotations

import pytest

from tests.unit_tests.jsdriver import requires_node, run_node
from tests.unit_tests.test_template_comments import DAIV_DIR

pytestmark = requires_node

PROMPT_BOX_JS = DAIV_DIR / "sessions/static/sessions/js/prompt-box.js"

HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const { src, args, actions } = JSON.parse(readFileSync(0, "utf8"));
const components = {};
const listeners = {};
const requests = [];
const changes = [];
const observations = [];
const input = () => ({
  value: "", attributes: {},
  focus() {},
  setAttribute(name, value) { this.attributes[name] = value; },
});
const refs = {
  repoSearch: input(), branchSearch: input(),
  repoPickerList: { innerHTML: "", scrollTop: 0 },
  branchPickerList: { innerHTML: "", scrollTop: 0 },
};
const sentinel = {
  matches: (selector) => selector === "[data-picker-sentinel]",
  replaceWith() {},
};
const sandbox = {
  document: { addEventListener: (name, cb) => { if (name === "alpine:init") cb(); } },
  surfaceGroup: { join: () => () => {} },
  pickerError: { showIn: (list) => { list.innerHTML = "error"; }, row: () => ({}) },
  setTimeout: () => 1, clearTimeout() {},
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
  dispatchEvent: (event) => changes.push(event.detail),
  htmx: {
    process() {},
    trigger: (target, event) => requests.push({
      picker: target === refs.repoSearch ? "repo" : "branch",
      query: target.value,
      url: target.attributes["hx-get"] || "",
      event,
    }),
  },
};
sandbox.window = sandbox;
sandbox.Alpine = { data: (name, factory) => { components[name] = factory; } };
vm.createContext(sandbox);
vm.runInContext(readFileSync(src, "utf8"), sandbox);
const picker = components.promptBox(args);
picker.$refs = refs;
picker.$nextTick = (cb) => cb();
picker.$el = { addEventListener: (name, cb) => { listeners[name] = cb; } };
picker.init();

for (const [method, ...params] of actions) {
  if (method === "search") refs.repoSearch.value = params[0];
  else if (method === "loaded") {
    refs.repoPickerList.innerHTML = params[0] || "<ul>repositories</ul>";
    listeners["htmx:afterSwap"]({ target: refs.repoPickerList });
  } else if (method === "scroll") refs.repoPickerList.scrollTop = params[0];
  else if (method === "event") {
    listeners[params[0]]({ target: params[1] === "sentinel" ? sentinel : refs[params[1]] });
  } else if (method === "observe") {
    const [name, ...values] = params;
    observations.push(picker[name]?.(...values) ?? null);
  } else picker[method](...params);
}
process.stdout.write(JSON.stringify({
  repos: picker.repos, popover: picker.popover, editingIndex: picker.editingIndex,
  repoLoading: picker.repoLoading, conflictMessage: picker.conflictMessage,
  query: refs.repoSearch.value, results: refs.repoPickerList.innerHTML,
  scrollTop: refs.repoPickerList.scrollTop, requests, changes, observations,
}));
"""


def drive(actions: list[list], **overrides) -> dict:
    return run_node(
        HARNESS,
        {
            "src": str(PROMPT_BOX_JS),
            "args": {"maxRepos": 20, "onChangeEvent": "repos-changed", **overrides},
            "actions": actions,
        },
    )


def test_multiple_additions_keep_the_picker_open_and_publish_each_selection():
    state = drive([["openRepoPicker"], ["loaded"], ["setRepo", "acme/api", "main"], ["setRepo", "acme/web", "dev"]])

    assert state["popover"] == "repo"
    assert state["repos"] == [{"slug": "acme/api", "ref": "main"}, {"slug": "acme/web", "ref": "dev"}]
    assert state["changes"] == [
        {"repos": [{"repo_id": "acme/api", "ref": "main"}]},
        {"repos": [{"repo_id": "acme/api", "ref": "main"}, {"repo_id": "acme/web", "ref": "dev"}]},
    ]


def test_reopening_keeps_search_results_and_scroll_without_another_request():
    state = drive([
        ["openRepoPicker"],
        ["search", "acme"],
        ["loaded", "<ul>three pages of acme repositories</ul>"],
        ["scroll", 220],
        ["setRepo", "acme/api", "main"],
        ["closePopover"],
        ["openRepoPicker"],
    ])

    assert state["query"] == "acme"
    assert state["results"] == "<ul>three pages of acme repositories</ul>"
    assert state["scrollTop"] == 220
    assert state["repoLoading"] is False
    assert len(state["requests"]) == 1


def test_reopening_during_an_inflight_request_does_not_start_another():
    state = drive([["openRepoPicker"], ["closePopover"], ["openRepoPicker"]])

    assert state["repoLoading"] is True
    assert len(state["requests"]) == 1


@pytest.mark.parametrize("event", ["htmx:sendError", "htmx:responseError"])
def test_failed_search_retries_on_reopen_without_clearing_the_query(event):
    state = drive([
        ["openRepoPicker"],
        ["search", "acme"],
        ["event", event, "repoSearch"],
        ["closePopover"],
        ["openRepoPicker"],
    ])

    assert state["query"] == "acme"
    assert state["requests"][-1]["query"] == "acme"
    assert len(state["requests"]) == 2
    assert state["repoLoading"] is True


def test_failed_pagination_retries_the_current_search_on_reopen():
    state = drive([
        ["openRepoPicker"],
        ["search", "acme"],
        ["loaded"],
        ["event", "htmx:responseError", "sentinel"],
        ["closePopover"],
        ["openRepoPicker"],
    ])

    assert len(state["requests"]) == 2
    assert state["requests"][-1]["query"] == "acme"


def test_reselecting_the_same_repository_and_branch_does_not_interrupt_adding():
    state = drive([["openRepoPicker"], ["loaded"], ["setRepo", "acme/api", "main"], ["setRepo", "acme/api", "main"]])

    assert state["popover"] == "repo"
    assert state["repos"] == [{"slug": "acme/api", "ref": "main"}]
    assert len(state["changes"]) == 1


def test_reaching_the_limit_keeps_the_picker_open_and_blocks_further_additions():
    state = drive(
        [
            ["openRepoPicker"],
            ["setRepo", "acme/api", "main"],
            ["setRepo", "acme/web", "main"],
            ["setRepo", "acme/extra", "main"],
            ["observe", "canSelectRepo", "acme/extra", "main"],
        ],
        maxRepos=2,
    )

    assert state["popover"] == "repo"
    assert state["repos"] == [{"slug": "acme/api", "ref": "main"}, {"slug": "acme/web", "ref": "main"}]
    assert len(state["changes"]) == 2
    assert state["observations"] == [False]


def test_single_repository_choice_still_closes_the_picker():
    state = drive([["openRepoPicker"], ["setRepo", "acme/api", "main"]], maxRepos=1)

    assert state["popover"] is None
    assert state["repos"] == [{"slug": "acme/api", "ref": "main"}]
    assert state["changes"] == [{"repos": [{"repo_id": "acme/api", "ref": "main"}]}]


def test_replacing_an_entry_is_allowed_at_the_limit_and_closes_the_picker():
    state = drive(
        [["openRepoPicker", 0], ["observe", "canSelectRepo", "acme/web", "main"], ["setRepo", "acme/web", "main"]],
        maxRepos=1,
        initialRepos=[{"repo_id": "acme/api", "ref": "main"}],
    )

    assert state["observations"] == [True]
    assert state["repos"] == [{"slug": "acme/web", "ref": "main"}]
    assert state["popover"] is None


def test_selection_state_updates_when_a_chip_is_removed():
    state = drive(
        [
            ["observe", "isRepoSelected", "acme/api"],
            ["observe", "canSelectRepo", "acme/api", "main"],
            ["observe", "selectedBranches", "acme/api", "default"],
            ["remove", 0],
            ["observe", "isRepoSelected", "acme/api"],
            ["observe", "canSelectRepo", "acme/api", "main"],
        ],
        initialRepos=[{"repo_id": "acme/api", "ref": "main"}],
    )

    assert state["observations"] == [True, False, "main", False, True]


def test_a_repository_can_be_added_on_another_branch_and_shows_all_selected_branches():
    state = drive(
        [
            ["openRepoPicker"],
            ["observe", "canSelectRepo", "acme/api", "main"],
            ["setRepo", "acme/api", "main"],
            ["observe", "selectedBranches", "acme/api", "default"],
        ],
        initialRepos=[{"repo_id": "acme/api", "ref": "feature"}],
    )

    assert state["observations"] == [True, "feature, main"]
    assert state["repos"] == [{"slug": "acme/api", "ref": "feature"}, {"slug": "acme/api", "ref": "main"}]


def test_an_unspecified_branch_uses_the_translated_default_label():
    state = drive(
        [["observe", "selectedBranches", "acme/api", "predefinida"]], initialRepos=[{"repo_id": "acme/api", "ref": ""}]
    )

    assert state["observations"] == ["predefinida"]


def test_an_unspecified_branch_prevents_adding_the_same_default_branch_again():
    state = drive(
        [["openRepoPicker"], ["observe", "canSelectRepo", "acme/api", "main"], ["setRepo", "acme/api", "main"]],
        initialRepos=[{"repo_id": "acme/api", "ref": ""}],
    )

    assert state["observations"] == [False]
    assert state["repos"] == [{"slug": "acme/api", "ref": ""}]
    assert state["changes"] == []


def test_replacement_allows_the_current_entry_but_not_another_identical_entry():
    state = drive(
        [
            ["openRepoPicker", 0],
            ["observe", "canSelectRepo", "acme/api", "main"],
            ["observe", "canSelectRepo", "acme/web", "main"],
        ],
        initialRepos=[{"repo_id": "acme/api", "ref": "main"}, {"repo_id": "acme/web", "ref": "main"}],
    )

    assert state["observations"] == [True, False]


def test_branch_selection_keeps_repository_search_context_for_the_next_addition():
    state = drive([
        ["openRepoPicker"],
        ["search", "acme"],
        ["loaded"],
        ["setRepo", "acme/api", "main"],
        ["openBranchPicker", 0],
        ["setBranch", "feature"],
        ["openRepoPicker"],
    ])

    assert state["query"] == "acme"
    assert state["repos"] == [{"slug": "acme/api", "ref": "feature"}]
    assert [request["picker"] for request in state["requests"]] == ["repo", "branch"]
