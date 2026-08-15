from unittest.mock import MagicMock, patch

from memory.llm import build_structured_llm
from memory.schemas import MemoryOperations


def _models(*models):
    return patch("automation.agent.base.BaseAgent.get_model", side_effect=list(models))


def test_build_structured_llm_adds_no_retry_layer():
    # A retry re-sends a byte-identical request, and the dominant failure is a None result rather
    # than an exception, so the layer never helped. sessions.classification keeps its retry
    # deliberately — do not reconcile the two by re-adding one here.
    model = MagicMock()

    with _models(model):
        chain = build_structured_llm(MemoryOperations, ["primary"])

    model.with_structured_output.assert_called_once_with(MemoryOperations)
    model.with_structured_output.return_value.with_retry.assert_not_called()
    assert chain is model.with_structured_output.return_value


def test_build_structured_llm_wraps_the_primary_in_the_remaining_models():
    primary, fallback = MagicMock(), MagicMock()

    with _models(primary, fallback) as get_model:
        chain = build_structured_llm(MemoryOperations, ["primary", "fallback"])

    assert [call.kwargs["model"] for call in get_model.call_args_list] == ["primary", "fallback"]
    primary.with_structured_output.return_value.with_fallbacks.assert_called_once_with([
        fallback.with_structured_output.return_value
    ])
    assert chain is primary.with_structured_output.return_value.with_fallbacks.return_value
