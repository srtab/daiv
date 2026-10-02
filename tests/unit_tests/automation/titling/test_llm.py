from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from automation.agent.base import BaseAgent
from automation.titling.llm import GeneratedTitle, TitlerNotConfiguredError, _ref_is_informative, generate_title


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("", False),
        ("main", False),
        ("MAIN", False),
        ("master", False),
        ("develop", False),
        ("prod", False),
        ("Production", False),
        ("a1b2c3d", False),  # 7-char SHA
        ("DEADBEEFCAFE1234567890ABCDEF1234567890AB", False),  # 40-char SHA, mixed case
        ("feat/copilotkit-chat", True),
        ("bugfix-123", True),
        ("a1b2c3", True),  # 6 chars — too short for SHA pattern
        ("g1h2i3j4", True),  # contains non-hex chars
        ("release/2026-04", True),
    ],
)
def test_ref_is_informative(ref: str, expected: bool):
    assert _ref_is_informative(ref) is expected


def _fake_chain(title: str = "Generated test title", capture: dict | None = None):
    """Build a Mock that mimics ``llm.with_structured_output(...).with_retry(...).with_fallbacks(...)``."""
    chain = MagicMock()
    chain.with_structured_output.return_value = chain
    chain.with_retry.return_value = chain
    chain.with_fallbacks.return_value = chain
    chain.with_config.return_value = chain

    def _invoke(messages):
        if capture is not None:
            capture["messages"] = messages
        return GeneratedTitle(title=title)

    chain.invoke.side_effect = _invoke
    return chain


def _generate(chain, prompt: str = "add login", **context) -> str:
    with patch.object(BaseAgent, "get_model", return_value=chain):
        return generate_title(prompt, run_metadata={"entity_type": "run"}, **context)


def test_returns_the_generated_title():
    assert _generate(_fake_chain(title="Add login feature")) == "Add login feature"


def test_user_text_includes_branch_when_informative():
    capture: dict = {}
    _generate(_fake_chain(capture=capture), repo_id="group/repo", ref="feat/copilotkit-chat")

    human_text = capture["messages"][-1].content
    assert "Repository: group/repo" in human_text
    assert "Branch: feat/copilotkit-chat" in human_text
    assert "Task: add login" in human_text


def test_user_text_omits_branch_for_generic_ref():
    capture: dict = {}
    _generate(_fake_chain(capture=capture), repo_id="group/repo", ref="main")

    assert "Branch:" not in capture["messages"][-1].content


def test_user_text_omits_repo_and_branch_when_not_given():
    """Batch titling spans repos, so it passes neither."""
    capture: dict = {}
    _generate(_fake_chain(capture=capture))

    human_text = capture["messages"][-1].content
    assert "Repository:" not in human_text
    assert "Branch:" not in human_text
    assert "Task: add login" in human_text


def test_prompt_truncated_to_500_chars():
    capture: dict = {}
    _generate(_fake_chain(capture=capture), prompt="x" * 1000)

    human_text = capture["messages"][-1].content
    assert human_text.endswith("x" * 500)
    assert "x" * 501 not in human_text


def test_calls_the_model_once():
    chain = _fake_chain()
    _generate(chain)

    assert chain.invoke.call_count == 1


def test_a_model_that_cannot_be_built_is_reported_as_not_configured():
    with (
        patch.object(BaseAgent, "get_model", side_effect=RuntimeError("no key")),
        pytest.raises(TitlerNotConfiguredError),
    ):
        generate_title("any", run_metadata={})


def test_a_failing_model_call_is_not_mistaken_for_a_missing_model():
    chain = _fake_chain()
    chain.invoke.side_effect = RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down") as excinfo:
        _generate(chain)

    assert not isinstance(excinfo.value, TitlerNotConfiguredError)
