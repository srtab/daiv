"""Tests for the swappable classification *method* (``sessions.classification``).

The task-side gating/invariants are covered in ``test_classify_task.py``; here we pin the method
module directly: the status-vocabulary parity with ``EnvelopeStatus``, the structured-output chain
assembly (primary + optional fallbacks), and the message/trace-metadata wiring.
"""

from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import HumanMessage, SystemMessage
from sessions.classification import SYSTEM_PROMPT, RunClassification, _build_structured_llm, classify_response_text
from sessions.models import EnvelopeStatus

from schedules.models import Intent


def test_status_literal_matches_envelope_status_minus_failed():
    """``RunClassification.status`` must stay a subset of ``EnvelopeStatus`` (minus ``FAILED``, which
    the model never authors) so ``EnvelopeStatus(draft.status)`` in the task can never raise. Mirrors
    the enum/literal parity guards elsewhere in the codebase (e.g. the RunEnvelope status constraint)."""
    literal_values = set(get_args(RunClassification.model_fields["status"].annotation))
    assert literal_values == set(EnvelopeStatus.values) - {EnvelopeStatus.FAILED}


def test_build_structured_llm_single_model_skips_fallbacks():
    model = MagicMock()
    chain = model.with_structured_output.return_value.with_retry.return_value

    with patch("sessions.classification.BaseAgent.get_model", return_value=model) as get_model:
        result = _build_structured_llm(RunClassification, ("only",))

    get_model.assert_called_once_with(model="only")
    model.with_structured_output.assert_called_once_with(RunClassification)
    model.with_structured_output.return_value.with_retry.assert_called_once_with(stop_after_attempt=2)
    chain.with_fallbacks.assert_not_called()
    assert result is chain


def test_build_structured_llm_multi_model_wraps_primary_in_fallbacks():
    model = MagicMock()
    chain = model.with_structured_output.return_value.with_retry.return_value

    with patch("sessions.classification.BaseAgent.get_model", return_value=model) as get_model:
        result = _build_structured_llm(RunClassification, ("primary", "fallback"))

    # Primary is built first, then each fallback; the primary chain is wrapped with the fallback list.
    assert [call.kwargs["model"] for call in get_model.call_args_list] == ["primary", "fallback"]
    chain.with_fallbacks.assert_called_once()
    (fallbacks_arg,) = chain.with_fallbacks.call_args.args
    assert len(fallbacks_arg) == 1
    assert result is chain.with_fallbacks.return_value


async def test_classify_response_text_assembles_messages_and_trace_metadata():
    classification = RunClassification(status="all-clear", summary="ok", actionable=[])
    llm = MagicMock()
    configured = llm.with_config.return_value
    configured.ainvoke = AsyncMock(return_value=classification)

    with patch("sessions.classification._build_structured_llm", return_value=llm):
        result = await classify_response_text("the prose", intent=Intent.REPORT, model_names=("m",))

    assert result is classification
    # ``intent`` flows into trace metadata only.
    assert llm.with_config.call_args.kwargs["metadata"] == {"intent": str(Intent.REPORT)}
    messages = configured.ainvoke.call_args.args[0]
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == SYSTEM_PROMPT
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content == "the prose"
