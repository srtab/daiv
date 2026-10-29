import logging
from unittest.mock import Mock, patch

import pytest

from automation.agent.configured_models import build_configured_model, report_model_fallback
from automation.agent.notices import ModelFallbackNotice

EXCEPTION_TEXT = "raw exception detail"


class _Notices:
    def __init__(self):
        self.recorded: list[ModelFallbackNotice] = []

    def record(self, notice: ModelFallbackNotice) -> None:
        self.recorded.append(notice)

    async def aflush(self) -> None:
        raise AssertionError("report_model_fallback must not flush")


def _build(get_model: Mock, spec: str = "openrouter:vendor/model"):
    with patch("automation.agent.configured_models.get_model", get_model):
        return build_configured_model(spec=spec, thinking_level="high")


def test_builds_the_spec_with_the_given_thinking_level():
    model = object()
    get_model = Mock(return_value=model)

    assert _build(get_model) == (model, None)
    get_model.assert_called_once_with(model="openrouter:vendor/model", thinking_level="high")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ValueError(f"Unknown provider prefix {EXCEPTION_TEXT}"), "unknown_provider"),
        (RuntimeError(f"Provider disabled {EXCEPTION_TEXT}"), "provider_unavailable"),
    ],
    ids=["unknown-provider", "provider-unavailable"],
)
def test_a_spec_that_cannot_be_used_returns_its_reason_without_logging(caplog, error, reason):
    with caplog.at_level(logging.DEBUG):
        assert _build(Mock(side_effect=error)) == (None, reason)

    assert caplog.records == []


def test_any_other_failure_is_init_failed_and_keeps_its_traceback_in_the_log(caplog):
    with caplog.at_level(logging.ERROR, logger="daiv.agent"):
        assert _build(Mock(side_effect=TypeError(EXCEPTION_TEXT))) == (None, "init_failed")

    assert [record.exc_info is not None for record in caplog.records] == [True]
    assert "openrouter:vendor/model" in caplog.text


def _report(notices, reason="unknown_provider"):
    report_model_fallback(
        notices,
        target="skill",
        name="code-review",
        configured_model="nosuch:model",
        served_model="openrouter:run/model",
        reason=reason,
    )


def test_a_fallback_is_logged_at_error_with_every_field(caplog):
    with caplog.at_level(logging.ERROR, logger="daiv.agent"):
        _report(None)

    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    for field in ("skill", "code-review", "nosuch:model", "openrouter:run/model", "unknown_provider"):
        assert field in caplog.text


def test_a_fallback_is_recorded_on_the_notices():
    notices = _Notices()

    _report(notices, reason="init_failed")

    assert notices.recorded == [
        ModelFallbackNotice(
            target="skill",
            name="code-review",
            configured_model="nosuch:model",
            served_model="openrouter:run/model",
            reason="init_failed",
        )
    ]
    assert notices.recorded[0].as_dict() == {
        "kind": "model_fallback",
        "target": "skill",
        "name": "code-review",
        "configured_model": "nosuch:model",
        "served_model": "openrouter:run/model",
        "reason": "init_failed",
    }


def test_the_notice_never_carries_exception_text():
    notices = _Notices()
    _model, reason = _build(Mock(side_effect=RuntimeError(EXCEPTION_TEXT)))
    assert reason is not None

    _report(notices, reason=reason)

    assert EXCEPTION_TEXT not in str(notices.recorded[0].as_dict())
