import pytest
from langchain_core.messages import HumanMessage
from sessions.executor.lock import Held, NoLock, Wait
from sessions.executor.spec import RunSpec

from codebase.base import Scope


def _one_shot(**overrides) -> RunSpec:
    fields = {
        "thread_id": None,
        "repo_id": "owner/repo",
        "scope": Scope.GLOBAL,
        "input_messages": (HumanMessage(content="hi"),),
        "trigger": "eval",
        "lock": NoLock(),
    }
    return RunSpec(**(fields | overrides))


def test_a_one_shot_run_needs_no_session():
    assert _one_shot().thread_id is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"lock": Wait(holder_id="run-1", timeout_s=1)},
        {"lock": Held(holder_id="run-1")},
        {"run_id": "run-1"},
        {"persist_ref": True},
        {"arm_watch": True},
        {"recover_draft": True},
        {"fallback_ref_on_missing": True},
    ],
    ids=["wait", "held", "run-id", "persist-ref", "arm-watch", "recover-draft", "fallback-ref"],
)
def test_a_one_shot_run_refuses_what_needs_a_session(overrides):
    with pytest.raises(ValueError, match="one-shot run"):
        _one_shot(**overrides)
