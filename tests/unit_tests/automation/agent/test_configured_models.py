import logging
from unittest.mock import Mock, patch

import pytest

from automation.agent.agent_settings import ModelChain
from automation.agent.configured_models import ConfiguredModels, build_configured_model
from automation.agent.model_spec import ModelSpecError
from automation.agent.notices import ModelFallbackNotice
from tests.unit_tests.automation.agent.configured_model_fakes import FakeModels, FakeNotices

EXCEPTION_TEXT = "raw exception detail"
RUN_MODEL = Mock(name="run-model")
CHAIN = ModelChain(names=("fake:run",), thinking_level="high")


def _build(get_model: Mock, spec: str = "openrouter:vendor/model"):
    with patch("automation.agent.configured_models.get_model", get_model):
        return build_configured_model(spec=spec, thinking_level="high")


def test_builds_the_spec_with_the_given_thinking_level():
    model = object()
    get_model = Mock(return_value=model)

    assert _build(get_model) is model
    get_model.assert_called_once_with(model="openrouter:vendor/model", thinking_level="high")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ModelSpecError(f"Unknown provider prefix {EXCEPTION_TEXT}"), "unknown_provider"),
        (RuntimeError(f"Provider disabled {EXCEPTION_TEXT}"), "provider_unavailable"),
    ],
    ids=["unknown-provider", "provider-unavailable"],
)
def test_a_spec_that_cannot_be_used_returns_its_reason_without_logging(caplog, error, reason):
    with caplog.at_level(logging.DEBUG):
        assert _build(Mock(side_effect=error)) == reason

    assert caplog.records == []


@pytest.mark.parametrize("error", [TypeError(EXCEPTION_TEXT), ValueError(EXCEPTION_TEXT)], ids=["type", "value"])
def test_any_other_failure_is_init_failed_and_keeps_its_traceback_in_the_log(caplog, error):
    with caplog.at_level(logging.ERROR, logger="daiv.agent"):
        assert _build(Mock(side_effect=error)) == "init_failed"

    assert [record.exc_info is not None for record in caplog.records] == [True]
    assert "openrouter:vendor/model" in caplog.text


def _for_subagent(models: FakeModels, *, configured: dict, file_spec: str = "", notices=None):
    configured_models = ConfiguredModels("subagent", configured, chain=CHAIN, notices=notices)
    with patch("automation.agent.configured_models.get_model", models):
        return configured_models.for_subagent("cr-tests", file_spec, RUN_MODEL)


async def _dispatched(
    models: FakeModels, *, configured: dict, file_spec: str = "", notices=None, never_skip: bool = False
):
    """``cr-tests``'s model, built and then dispatched as many times as a run might."""
    configured_models = ConfiguredModels("subagent", configured, chain=CHAIN, notices=notices)
    with patch("automation.agent.configured_models.get_model", models):
        served = configured_models.for_subagent("cr-tests", file_spec, RUN_MODEL, never_skip=never_skip)
    for _ in range(2):
        await configured_models.areport_dispatch("cr-tests")
    return served


class TestForSubagent:
    def test_an_entry_beats_the_files_model_and_runs_with_the_runs_thinking_level(self):
        models = FakeModels("fake:file", "fake:configured")

        served = _for_subagent(models, configured={"cr-tests": "fake:configured"}, file_spec="fake:file")

        assert served is models.by_spec["fake:configured"]
        assert models.calls == [("fake:configured", "high")]

    @pytest.mark.parametrize("file_spec", ["fake:file", ""], ids=["file", "run"])
    def test_without_an_entry_the_files_model_else_the_runs_serves(self, file_spec):
        models = FakeModels("fake:file")

        served = _for_subagent(models, configured={"other": "fake:configured"}, file_spec=file_spec)

        assert served is (models.by_spec["fake:file"] if file_spec else RUN_MODEL)

    @pytest.mark.parametrize(
        ("file_spec", "served_spec"),
        [("fake:file", "fake:file"), ("", "fake:run"), ("broken:file", "fake:run")],
        ids=["file", "no-file-model", "broken-file-model"],
    )
    async def test_an_unusable_entry_falls_back_and_is_recorded_once_dispatched(self, caplog, file_spec, served_spec):
        models = FakeModels("fake:file")
        notices = FakeNotices()

        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            served = await _dispatched(
                models, configured={"cr-tests": "nosuch:model"}, file_spec=file_spec, notices=notices
            )

        assert served is (models.by_spec["fake:file"] if served_spec == "fake:file" else RUN_MODEL)
        assert notices.recorded == [
            ModelFallbackNotice(
                target="subagent",
                name="cr-tests",
                configured_model="nosuch:model",
                served_model=served_spec,
                reason="unknown_provider",
            )
        ]
        assert notices.flushed_with == [1]
        assert "nosuch:model" in caplog.text

    def test_an_unusable_entry_is_not_reported_while_the_subagent_is_only_built(self, caplog):
        notices = FakeNotices()

        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            _for_subagent(FakeModels(), configured={"cr-tests": "nosuch:model"}, notices=notices)

        assert (notices.recorded, caplog.records) == ([], [])

    def test_an_invalid_file_model_without_an_entry_raises(self):
        with pytest.raises(ValueError, match="broken:file"):
            _for_subagent(FakeModels(), configured={}, file_spec="broken:file")

    async def test_never_skip_serves_the_runs_model_for_an_invalid_file_model_and_records_it_once_dispatched(self):
        notices = FakeNotices()

        served = await _dispatched(
            FakeModels(), configured={}, file_spec="broken:file", notices=notices, never_skip=True
        )

        assert served is RUN_MODEL
        assert notices.recorded == [
            ModelFallbackNotice(
                target="subagent",
                name="cr-tests",
                configured_model="broken:file",
                served_model="fake:run",
                reason="unknown_provider",
            )
        ]

    def test_an_entry_shared_by_several_subagents_is_built_once(self):
        models = FakeModels("fake:configured")
        configured_models = ConfiguredModels(
            "subagent", {"cr-tests": "fake:configured", "cr-security": "fake:configured"}
        )

        with patch("automation.agent.configured_models.get_model", models):
            for name in ("cr-tests", "cr-security"):
                configured_models.for_subagent(name, "", RUN_MODEL)

        assert models.calls == [("fake:configured", None)]


async def test_a_fallback_without_notices_is_still_logged_at_error_with_every_field(caplog):
    with caplog.at_level(logging.ERROR, logger="daiv.agent"):
        await _dispatched(FakeModels(), configured={"cr-tests": "nosuch:model"})

    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    for field in ("subagent", "cr-tests", "nosuch:model", "fake:run", "unknown_provider"):
        assert field in caplog.text


async def test_the_notice_never_carries_exception_text():
    notices = FakeNotices()

    await _dispatched(
        Mock(side_effect=RuntimeError(EXCEPTION_TEXT)), configured={"cr-tests": "nosuch:model"}, notices=notices
    )

    assert notices.recorded[0].as_dict() == {
        "kind": "model_fallback",
        "target": "subagent",
        "name": "cr-tests",
        "configured_model": "nosuch:model",
        "served_model": "fake:run",
        "reason": "provider_unavailable",
    }
