import pytest
from sessions.executor.lock import Held, Wait

from tests.unit_tests.sessions.executor.conftest import make_spec


def test_a_one_shot_run_without_session_switches_builds():
    make_spec(thread_id=None)


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
        make_spec(thread_id=None, **overrides)


def test_a_session_run_refuses_an_empty_thread_id():
    """Every run on ``""`` would share one checkpoint and one lock."""
    with pytest.raises(ValueError, match="non-empty thread_id"):
        make_spec(thread_id="")


@pytest.mark.parametrize("overrides", [{"agent_model": "other"}, {"use_max": True}], ids=["agent-model", "use-max"])
def test_an_exact_model_chain_refuses_the_switches_it_would_ignore(overrides):
    with pytest.raises(ValueError, match="model_names"):
        make_spec(model_names=("model-a",), **overrides)
