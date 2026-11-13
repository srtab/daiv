import logging
from unittest.mock import Mock, patch

import pytest

from automation.agent.agent_settings import ModelChain
from automation.agent.configured_models import ConfiguredModels
from core.model_choice import EMPTY_CHOICE, ModelChoice
from tests.unit_tests.automation.agent.configured_model_fakes import FakeModels

RUN_MODEL = Mock(name="run-model")
CHAIN = ModelChain(names=("fake:run",), thinking_level="high")


def _choices(specs: dict[str, str | ModelChoice]) -> dict[str, ModelChoice]:
    return {name: spec if isinstance(spec, ModelChoice) else ModelChoice(model=spec) for name, spec in specs.items()}


def _for_subagent(
    models: FakeModels,
    *,
    configured: dict,
    declared: ModelChoice = EMPTY_CHOICE,
    inherited: ModelChoice = EMPTY_CHOICE,
    never_skip: bool = False,
):
    configured_models = ConfiguredModels("subagent", _choices(configured), chain=CHAIN)
    with patch("automation.agent.configured_models.get_model", models):
        return configured_models.for_subagent(
            "cr-tests", declared, RUN_MODEL, inherited=inherited, never_skip=never_skip
        )


def _skill_choice(models: FakeModels, *, configured: dict, declared: ModelChoice = EMPTY_CHOICE):
    configured_models = ConfiguredModels("skill", _choices(configured), chain=CHAIN)
    with patch("automation.agent.configured_models.get_model", models):
        return configured_models.skill_choice("code-review", declared)


class TestForSubagent:
    def test_an_entry_beats_the_files_model_and_runs_with_the_runs_thinking_level(self):
        models = FakeModels("fake:file", "fake:configured")

        served = _for_subagent(
            models, configured={"cr-tests": "fake:configured"}, declared=ModelChoice(model="fake:file")
        )

        assert served is models.by_spec["fake:configured"]
        assert models.calls == [("fake:configured", "high")]

    @pytest.mark.parametrize("file_spec", ["fake:file", ""], ids=["file", "run"])
    def test_without_an_entry_the_files_model_else_the_runs_serves(self, file_spec):
        models = FakeModels("fake:file")

        served = _for_subagent(models, configured={"other": "fake:configured"}, declared=ModelChoice(model=file_spec))

        assert served is (models.by_spec["fake:file"] if file_spec else RUN_MODEL)

    @pytest.mark.parametrize(
        ("file_spec", "serves_file_model"),
        [("fake:file", True), ("", False), ("broken:file", False)],
        ids=["file", "no-file-model", "broken-file-model"],
    )
    def test_an_unusable_entry_falls_back_and_is_logged_at_error(self, caplog, file_spec, serves_file_model):
        models = FakeModels("fake:file")

        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            served = _for_subagent(
                models, configured={"cr-tests": "nosuch:model"}, declared=ModelChoice(model=file_spec)
            )

        assert served is (models.by_spec["fake:file"] if serves_file_model else RUN_MODEL)
        assert caplog.records[0].levelno == logging.ERROR
        assert "subagent model 'nosuch:model'" in caplog.records[0].getMessage()

    def test_an_invalid_file_model_without_an_entry_raises(self):
        with pytest.raises(ValueError, match="broken:file"):
            _for_subagent(FakeModels(), configured={}, declared=ModelChoice(model="broken:file"))

    def test_never_skip_serves_the_runs_model_for_an_invalid_file_model(self, caplog):
        with caplog.at_level(logging.WARNING, logger="daiv.agent"):
            served = _for_subagent(
                FakeModels(), configured={}, declared=ModelChoice(model="broken:file"), never_skip=True
            )

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
            "subagent", _choices({"cr-tests": "fake:configured", "cr-security": "fake:configured"})
        )

        with patch("automation.agent.configured_models.get_model", models):
            for name in ("cr-tests", "cr-security"):
                configured_models.for_subagent(name, EMPTY_CHOICE, RUN_MODEL)

        assert models.calls == [("fake:configured", None)]


class TestForSubagentLevels:
    def test_an_entry_level_without_a_model_builds_the_runs_model_at_that_level(self):
        models = FakeModels("fake:run")

        served = _for_subagent(models, configured={"cr-tests": ModelChoice(thinking_level="low")})

        assert served is models.by_spec["fake:run"]
        assert models.calls == [("fake:run", "low")]

    def test_the_declared_level_applies_to_an_entry_model(self):
        models = FakeModels("fake:configured")

        _for_subagent(
            models,
            configured={"cr-tests": ModelChoice(model="fake:configured")},
            declared=ModelChoice(thinking_level="xhigh"),
        )

        assert models.calls == [("fake:configured", "xhigh")]

    def test_a_detector_inherits_the_skills_model_and_level(self):
        models = FakeModels("fake:skill")

        _for_subagent(models, configured={}, inherited=ModelChoice("fake:skill", "medium"))

        assert models.calls == [("fake:skill", "medium")]

    def test_a_detector_level_beats_the_skills(self):
        models = FakeModels("fake:skill")

        _for_subagent(
            models,
            configured={"cr-tests": ModelChoice(thinking_level="xhigh")},
            inherited=ModelChoice("fake:skill", "low"),
        )

        assert models.calls == [("fake:skill", "xhigh")]

    def test_the_charter_beats_the_skill(self):
        models = FakeModels("fake:charter")

        _for_subagent(
            models,
            configured={},
            declared=ModelChoice("fake:charter", "low"),
            inherited=ModelChoice("fake:skill", "high"),
        )

        assert models.calls == [("fake:charter", "low")]

    def test_off_stops_inheritance(self):
        models = FakeModels("fake:run")

        _for_subagent(
            models,
            configured={"cr-tests": ModelChoice(thinking_level="off")},
            inherited=ModelChoice(thinking_level="high"),
        )

        assert models.calls == [("fake:run", None)]

    def test_nothing_set_serves_the_run_model_instance(self):
        models = FakeModels()

        assert _for_subagent(models, configured={}) is RUN_MODEL
        assert models.calls == []

    def test_an_unbuildable_inherited_model_falls_back_to_the_run(self):
        served = _for_subagent(FakeModels(), configured={}, inherited=ModelChoice(model="nosuch:model"))

        assert served is RUN_MODEL

    def test_a_declared_model_without_a_level_runs_at_the_runs_level(self):
        models = FakeModels("fake:file")

        _for_subagent(models, configured={}, declared=ModelChoice(model="fake:file"), never_skip=True)

        assert models.calls == [("fake:file", "high")]

    def test_a_level_only_entry_without_a_chain_keeps_the_run_model(self):
        configured_models = ConfiguredModels("subagent", {"cr-tests": ModelChoice(thinking_level="low")})

        with patch("automation.agent.configured_models.get_model", FakeModels()):
            assert configured_models.for_subagent("cr-tests", EMPTY_CHOICE, RUN_MODEL) is RUN_MODEL


class TestSkillChoice:
    def test_an_entry_beats_the_files_model(self):
        models = FakeModels("fake:file", "fake:configured")

        choice = _skill_choice(
            models, configured={"code-review": "fake:configured"}, declared=ModelChoice(model="fake:file")
        )

        assert choice.model == "fake:configured"

    def test_without_an_entry_the_files_model_serves(self):
        choice = _skill_choice(FakeModels("fake:file"), configured={}, declared=ModelChoice(model="fake:file"))

        assert choice.model == "fake:file"

    @pytest.mark.parametrize(
        ("file_spec", "served"),
        [("fake:file", "fake:file"), (None, None), ("broken:file", None)],
        ids=["file", "no-file-model", "broken-file-model"],
    )
    def test_an_unusable_entry_falls_back_and_is_logged_at_error(self, caplog, file_spec, served):
        with caplog.at_level(logging.ERROR, logger="daiv.agent"):
            choice = _skill_choice(
                FakeModels("fake:file"),
                configured={"code-review": "nosuch:model"},
                declared=ModelChoice(model=file_spec or ""),
            )

        assert choice.model == (served or "")
        assert "skill model 'nosuch:model'" in caplog.records[0].getMessage()

    def test_an_unusable_spec_is_built_and_logged_once_per_run(self, caplog):
        models = FakeModels()
        configured_models = ConfiguredModels("skill", _choices({"code-review": "nosuch:model"}), chain=CHAIN)

        with (
            caplog.at_level(logging.ERROR, logger="daiv.agent"),
            patch("automation.agent.configured_models.get_model", models),
        ):
            for _ in range(2):
                assert configured_models.skill_choice("code-review", EMPTY_CHOICE) == EMPTY_CHOICE

        assert (models.calls, len(caplog.records)) == ([("nosuch:model", "high")], 1)

    def test_the_entry_level_combines_with_the_declared_model(self):
        choice = _skill_choice(
            FakeModels("fake:file"),
            configured={"code-review": ModelChoice(thinking_level="low")},
            declared=ModelChoice(model="fake:file"),
        )

        assert choice == ModelChoice("fake:file", "low")

    def test_an_unbuildable_model_is_dropped_but_its_level_kept(self):
        choice = _skill_choice(FakeModels(), configured={"code-review": ModelChoice("nosuch:model", "high")})

        assert choice == ModelChoice(thinking_level="high")

    def test_nothing_set_is_empty(self):
        assert _skill_choice(FakeModels(), configured={}) == EMPTY_CHOICE
