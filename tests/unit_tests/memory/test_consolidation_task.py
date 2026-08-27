from unittest.mock import patch

import pytest
from memory.models import ObservationStatus, RepositoryMemory
from memory.schemas import MemoryOperation
from memory.tasks import consolidate_memory_task

from tests.unit_tests.memory.consolidation_helpers import (
    _enabled_config,
    _entry,
    _observation,
    _site_settings,
    _structured_llm_returning,
)


async def _run_round(llm, config=None):
    with (
        patch("memory.tasks.RepositoryConfig") as cfg,
        patch("memory.tasks.site_settings", _site_settings()),
        patch("memory.consolidation.build_structured_llm", return_value=llm),
        patch("memory.consolidation.site_settings", _site_settings()),
    ):
        cfg.get_config.return_value = config or _enabled_config()
        await consolidate_memory_task.func("group/project")


@pytest.mark.django_db(transaction=True)
class TestDegradedRoundReporting:
    async def test_observations_the_model_never_named_are_reported_as_a_warning(self, caplog):
        # These stay pending and are retried forever if the model keeps skipping them. Logged as a
        # warning so the signal stays in the logs without minting a Sentry event each cycle.
        claimed = await _observation(content="the one the model handled")
        ignored = await _observation(content="the one the model forgot")
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(claimed.pk)], category="pitfall", content="a new fact")
        )

        with caplog.at_level("WARNING", logger="daiv.memory"):
            await _run_round(llm)

        assert "left 1 of 2 observation(s) unclaimed" in caplog.text
        await ignored.arefresh_from_db()
        assert ignored.status == ObservationStatus.PENDING

    async def test_mostly_rejected_round_is_reported_as_a_warning(self, caplog):
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="the only good one"),
            MemoryOperation(
                op="UPDATE", entry_ids=["not-a-real-id"], observation_ids=[str(obs.pk)], content="a revised fact"
            ),
            MemoryOperation(op="MERGE", entry_ids=["nope"], observation_ids=[str(obs.pk)], content="a merged fact"),
        )

        with caplog.at_level("WARNING", logger="daiv.memory"):
            await _run_round(llm)

        assert "rejected 2 of 3 operations" in caplog.text

    async def test_healthy_round_reports_no_error(self, caplog):
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="a new fact")
        )

        with caplog.at_level("ERROR", logger="daiv.memory"):
            await _run_round(llm)

        assert caplog.text == ""


@pytest.mark.django_db(transaction=True)
class TestLegacyDocumentGuard:
    async def test_round_refuses_to_overwrite_a_document_that_has_no_entries(self):
        # A repo consolidated before entries existed would lose its whole document to the
        # re-render, so the round bails out before the LLM call and stays retryable.
        await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- a legacy fact")
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="a brand new fact")
        )

        await _run_round(llm)

        llm.with_config.return_value.ainvoke.assert_not_awaited()
        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == "## Pitfalls\n- a legacy fact"
        assert memory.last_attempted_at is not None, "a wedged repo must back off, not retry hourly"
        from memory.models import MemoryEntry

        assert not await MemoryEntry.objects.aexists()
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING

    async def test_round_proceeds_once_the_document_has_entries_behind_it(self):
        await _entry("a legacy fact")
        await RepositoryMemory.objects.acreate(repo_id="group/project", content="## Pitfalls\n- a legacy fact")
        obs = await _observation()
        llm = _structured_llm_returning(
            MemoryOperation(op="ADD", observation_ids=[str(obs.pk)], category="pitfall", content="a brand new fact")
        )

        await _run_round(llm)

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == "## Pitfalls\n- a legacy fact\n- a brand new fact"
        assert memory.last_attempted_at is not None
        assert memory.last_consolidated_at is not None


@pytest.mark.django_db(transaction=True)
class TestAttemptRecording:
    """``last_attempted_at`` is the cron's only cooldown input, so every path must record one."""

    async def test_a_raising_round_still_records_the_attempt(self):
        await _observation()

        with (
            patch("memory.tasks.RepositoryConfig") as cfg,
            patch("memory.tasks.site_settings", _site_settings()),
            patch("memory.tasks.run_consolidation_round", side_effect=RuntimeError("provider 503")),
        ):
            cfg.get_config.return_value = _enabled_config()
            with pytest.raises(RuntimeError, match="provider 503"):
                await consolidate_memory_task.func("group/project")

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.last_attempted_at is not None, "a crashing round must still back the repo off"
        assert memory.last_consolidated_at is None


@pytest.mark.django_db(transaction=True)
class TestPreconditionsAndPrompt:
    async def test_noop_when_disabled_or_nothing_pending(self):
        with patch("memory.tasks.RepositoryConfig") as cfg, patch("memory.consolidation.build_structured_llm") as build:
            cfg.get_config.return_value = _enabled_config(enabled=False)
            await consolidate_memory_task.func("group/project")
            build.assert_not_called()

        with patch("memory.tasks.RepositoryConfig") as cfg, patch("memory.consolidation.build_structured_llm") as build:
            cfg.get_config.return_value = _enabled_config()
            await consolidate_memory_task.func("group/empty-repo")
            build.assert_not_called()
        assert not await RepositoryMemory.objects.filter(repo_id="group/empty-repo").aexists()

    async def test_noop_when_site_disabled(self):
        obs = await _observation()

        with (
            patch("memory.tasks.RepositoryConfig") as cfg,
            patch("memory.consolidation.build_structured_llm") as build,
            patch("memory.tasks.site_settings", _site_settings(memory_enabled=False)),
        ):
            cfg.get_config.return_value = _enabled_config(enabled=True)
            await consolidate_memory_task.func("group/project")

        build.assert_not_called()
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING

    @pytest.mark.parametrize(
        "exc",
        [RuntimeError("no api key"), ValueError("Unknown/Unsupported provider")],
        ids=["provider-unconfigured", "model-spec-invalid"],
    )
    async def test_noop_when_model_unavailable(self, exc):
        # build_structured_llm raising must skip, not crash: nothing consolidated, observations
        # stay pending. RuntimeError = provider disabled / no API key; ValueError = bad/unparseable
        # spec from parse_model_spec (regression guard for C1, whose original guard caught only
        # RuntimeError). The attempt is still recorded so the repo backs off instead of retrying hourly.
        obs = await _observation()

        with (
            patch("memory.tasks.RepositoryConfig") as cfg,
            patch("memory.consolidation.build_structured_llm", side_effect=exc),
        ):
            cfg.get_config.return_value = _enabled_config()
            await consolidate_memory_task.func("group/project")  # must not raise

        memory = await RepositoryMemory.objects.aget(repo_id="group/project")
        assert memory.content == ""
        assert memory.last_consolidated_at is None
        assert memory.last_attempted_at is not None, "a failed round must still back the repo off"
        await obs.arefresh_from_db()
        assert obs.status == ObservationStatus.PENDING

    @pytest.mark.parametrize(
        ("override_model", "expected_model"),
        [
            # Site override set → used as the primary model.
            ("openrouter:anthropic/claude-opus-4.6", "openrouter:anthropic/claude-opus-4.6"),
            # Empty override → reuse the repo agent model (config.models.agent.model).
            (None, "openrouter:anthropic/claude-sonnet-4.6"),
        ],
        ids=["site_override", "empty_reuses_repo_agent"],
    )
    async def test_model_selection(self, override_model, expected_model):
        await _observation()

        with (
            patch("memory.tasks.RepositoryConfig") as cfg,
            patch("memory.consolidation.build_structured_llm", return_value=_structured_llm_returning()) as build,
            patch("memory.consolidation.site_settings", _site_settings(memory_consolidation_model_name=override_model)),
        ):
            cfg.get_config.return_value = _enabled_config()
            await consolidate_memory_task.func("group/project")

        _schema, models = build.call_args.args
        assert models[0] == expected_model

    async def test_prompt_carries_entry_and_observation_ids(self):
        # The model can only target an entry by copying its ID back, so both lists must reach it.
        entry = await _entry("a fact already known")
        obs = await _observation(content="a fresh candidate observation")
        llm = _structured_llm_returning()

        await _run_round(llm)

        (messages,), _ = llm.with_config.return_value.ainvoke.call_args
        human_content = messages[-1].content
        assert str(entry.pk) in human_content
        assert "a fact already known" in human_content
        assert str(obs.pk) in human_content
        assert "a fresh candidate observation" in human_content
