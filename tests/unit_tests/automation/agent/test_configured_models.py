import logging
from unittest.mock import Mock, patch

import pytest

from automation.agent.agent_settings import ModelChain
from automation.agent.configured_models import ConfiguredModels
from tests.unit_tests.automation.agent.configured_model_fakes import FakeModels

RUN_MODEL = Mock(name="run-model")
CHAIN = ModelChain(names=("fake:run",), thinking_level="high")


def _for_subagent(models: FakeModels, *, configured: dict, file_spec: str = "", never_skip: bool = False):
    configured_models = ConfiguredModels("subagent", configured, chain=CHAIN)
    with patch("automation.agent.configured_models.get_model", models):
        return configured_models.for_subagent("cr-tests", file_spec, RUN_MODEL, never_skip=never_skip)


def _skill_spec(models: FakeModels, *, configured: dict, file_spec: str | None = None):
    configured_models = ConfiguredModels("skill", configured, chain=CHAIN)
    with patch("automation.agent.configured_models.get_model", models):
        return configured_models.skill_spec("code-review", file_spec)


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
        ("file_spec", "serves_file_model"),
        [("fake:file", True), ("", False), ("broken:file", False)],
        ids=["file", "no-file-model", "broken-file-model"],
    )
    def test_an_unusable_entry_falls_back_and_is_logged_at_error(self, caplog, file_spec, serves_file_model):
        models = FakeModels("fake:file")

        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            served = _for_subagent(models, configured={"cr-tests": "nosuch:model"}, file_spec=file_spec)

        assert served is (models.by_spec["fake:file"] if serves_file_model else RUN_MODEL)
        assert caplog.records[0].levelno == logging.ERROR
        assert "subagent model 'nosuch:model'" in caplog.records[0].getMessage()

    def test_an_invalid_file_model_without_an_entry_raises(self):
        with pytest.raises(ValueError, match="broken:file"):
            _for_subagent(FakeModels(), configured={}, file_spec="broken:file")

    def test_never_skip_serves_the_runs_model_for_an_invalid_file_model(self, caplog):
        with caplog.at_level(logging.WARNING, logger="daiv.agent"):
            served = _for_subagent(FakeModels(), configured={}, file_spec="broken:file", never_skip=True)

        assert served is RUN_MODEL
        assert [record.levelno for record in caplog.records] == [logging.WARNING]
        assert "broken:file" in caplog.text

    def test_an_unusable_entry_is_logged_at_error_once_across_runs(self, caplog):
        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            for _ in range(2):
                _for_subagent(FakeModels(), configured={"cr-tests": "nosuch:model"})

        assert len(caplog.records) == 1

    def test_an_entry_shared_by_several_subagents_is_built_once(self):
        models = FakeModels("fake:configured")
        configured_models = ConfiguredModels(
            "subagent", {"cr-tests": "fake:configured", "cr-security": "fake:configured"}
        )

        with patch("automation.agent.configured_models.get_model", models):
            for name in ("cr-tests", "cr-security"):
                configured_models.for_subagent(name, "", RUN_MODEL)

        assert models.calls == [("fake:configured", None)]


class TestSkillSpec:
    def test_an_entry_beats_the_files_model(self):
        models = FakeModels("fake:file", "fake:configured")

        assert _skill_spec(models, configured={"code-review": "fake:configured"}, file_spec="fake:file") == (
            "fake:configured"
        )

    def test_without_an_entry_the_files_model_serves(self):
        assert _skill_spec(FakeModels("fake:file"), configured={}, file_spec="fake:file") == "fake:file"

    @pytest.mark.parametrize(
        ("file_spec", "served"),
        [("fake:file", "fake:file"), (None, None), ("broken:file", None)],
        ids=["file", "no-file-model", "broken-file-model"],
    )
    def test_an_unusable_entry_falls_back_and_is_logged_at_error(self, caplog, file_spec, served):
        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            spec = _skill_spec(FakeModels("fake:file"), configured={"code-review": "nosuch:model"}, file_spec=file_spec)

        assert spec == served
        assert "skill model 'nosuch:model'" in caplog.records[0].getMessage()

    def test_an_unusable_spec_is_built_and_logged_once_per_run(self, caplog):
        models = FakeModels()
        configured_models = ConfiguredModels("skill", {"code-review": "nosuch:model"}, chain=CHAIN)

        with (
            caplog.at_level(logging.ERROR, logger="daiv.agent"),
            patch("automation.agent.configured_models.get_model", models),
        ):
            for _ in range(2):
                assert configured_models.skill_spec("code-review", None) is None

        assert (models.calls, len(caplog.records)) == ([("nosuch:model", "high")], 1)
