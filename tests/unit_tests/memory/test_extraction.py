import logging
from unittest.mock import patch

import pytest
from memory.extraction import extract_from_transcript, extract_observations
from memory.schemas import CONTENT_HARD_LIMIT, MAX_OBSERVATIONS, ExtractedObservation

from tests.unit_tests.memory.extraction_helpers import (
    TRANSCRIPT,
    _checkpointer_with,
    _checkpointer_with_delta,
    _create_run,
    _site_settings,
    _structured_llm_returning,
)


@pytest.mark.django_db(transaction=True)
async def test_extraction_reconstructs_deltachannel_messages():
    """deepagents' DeltaChannel keeps ``messages`` out of ``channel_values``; extraction
    must reconstruct the transcript from the delta write history, not skip as empty."""
    from langchain_core.messages import AIMessage, HumanMessage

    run = await _create_run()
    writes = [
        ("t0", "messages", [HumanMessage(content="fix the bug", id="h1")]),
        ("t1", "messages", [AIMessage(content="done, ran make test", id="a1")]),
    ]
    extracted = [ExtractedObservation(category="build_test", content="`make test` sets LANGCHAIN_TRACING_V2=false")]

    with (
        patch("core.checkpointer.open_checkpointer", _checkpointer_with_delta(writes)),
        patch("memory.extraction.build_structured_llm", return_value=_structured_llm_returning(extracted)),
        patch("memory.extraction.site_settings", _site_settings()),
    ):
        result = await extract_observations(run)

    assert len(result) == 1
    assert result[0].category == "build_test"


@pytest.mark.django_db(transaction=True)
async def test_extraction_skips_when_checkpoint_expired():
    run = await _create_run()

    with (
        patch("core.checkpointer.open_checkpointer", _checkpointer_with(None)),
        patch("memory.extraction.build_structured_llm") as build,
    ):
        assert await extract_observations(run) == []

    build.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_extraction_warns_when_checkpoint_has_no_messages(caplog):
    # A present checkpoint with an empty message list is a defect signature, distinct from
    # a missing/expired checkpoint: it skips like the expired case but logs at WARNING.
    run = await _create_run()

    with (
        patch("core.checkpointer.open_checkpointer", _checkpointer_with([])),
        patch("memory.extraction.build_structured_llm") as build,
        caplog.at_level("WARNING", logger="daiv.memory"),
    ):
        assert await extract_observations(run) == []

    build.assert_not_called()
    assert any("has no messages" in record.message for record in caplog.records)


@pytest.mark.django_db(transaction=True)
async def test_extraction_skips_transcript_without_ai_turns(caplog):
    # A run that died before the agent loop started (sandbox unavailable, auth failure) can only
    # produce an empty extraction — the model call is pure cost, so it is never made.
    from sessions.models import RunStatus

    run = await _create_run(status=RunStatus.FAILED)

    with (
        patch("core.checkpointer.open_checkpointer", _checkpointer_with([TRANSCRIPT[0]])),
        patch("memory.extraction.build_structured_llm") as build,
        caplog.at_level("INFO", logger="daiv.memory"),
    ):
        assert await extract_observations(run) == []

    build.assert_not_called()
    assert any("no AI turns" in record.message for record in caplog.records)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("fallback_model", "expected_models"),
    [
        # Both configured → both passed through.
        ("provider:m2", ("provider:m1", "provider:m2")),
        # Empty fallback is filtered out so build_structured_llm gets a 1-tuple, not (model, None).
        (None, ("provider:m1",)),
    ],
    ids=["with_fallback", "drops_empty_fallback"],
)
async def test_extraction_uses_configured_models(fallback_model, expected_models):
    run = await _create_run()
    extracted = [ExtractedObservation(category="build_test", content="`make test` needs the DB up first")]

    with (
        patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.extraction.build_structured_llm", return_value=_structured_llm_returning(extracted)) as build,
        patch(
            "memory.extraction.site_settings",
            _site_settings(
                memory_extraction_model_name="provider:m1", memory_extraction_fallback_model_name=fallback_model
            ),
        ),
    ):
        await extract_observations(run)

    _schema, models = build.call_args.args
    assert tuple(models) == expected_models


@pytest.mark.django_db(transaction=True)
async def test_extraction_noop_when_no_model_configured():
    # Both model and fallback empty (only reachable via an empty-string env override) → clean skip,
    # not an IndexError crash in build_structured_llm.
    run = await _create_run()

    with (
        patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.extraction.build_structured_llm") as build,
        patch(
            "memory.extraction.site_settings",
            _site_settings(memory_extraction_model_name="", memory_extraction_fallback_model_name=""),
        ),
    ):
        assert await extract_observations(run) == []

    build.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_extraction_noop_when_model_spec_invalid():
    # A bad/unparseable extraction model spec raises ValueError; it must be swallowed (clean skip),
    # not crash the task. The hardcoded extraction models raise this in a deployment without the
    # OpenAI/Anthropic provider rows configured (regression guard for C1).
    run = await _create_run()

    with (
        patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch(
            "memory.extraction.build_structured_llm", side_effect=ValueError("Unknown/Unsupported provider for model")
        ),
        patch("memory.extraction.site_settings", _site_settings()),
    ):
        assert await extract_observations(run) == []


async def _extract(run, extracted, caplog):
    with (
        patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.extraction.build_structured_llm", return_value=_structured_llm_returning(extracted)),
        patch("memory.extraction.site_settings", _site_settings()),
        caplog.at_level(logging.WARNING, logger="daiv.memory"),
    ):
        return await extract_observations(run)


@pytest.mark.django_db(transaction=True)
async def test_extraction_drops_unusable_observations_and_keeps_the_rest(caplog):
    # A field constraint would have failed the whole parse and lost the good observation with it.
    run = await _create_run()
    extracted = [
        ExtractedObservation(category="workflow", content="n/a"),
        ExtractedObservation(category="build_test", content="`make test` sets LANGCHAIN_TRACING_V2=false"),
        ExtractedObservation(category="pitfall", content="x" * (CONTENT_HARD_LIMIT + 1)),
    ]

    result = await _extract(run, extracted, caplog)

    assert [obs.category for obs in result] == ["build_test"]
    assert [record.levelname for record in caplog.records] == ["WARNING", "ERROR"]


@pytest.mark.django_db(transaction=True)
async def test_over_long_drop_is_an_error_carrying_the_content_it_lost(caplog):
    # Nothing re-queues these — no row is ever created and the transcript TTLs out — so the log is
    # the only trace, and a warning would never reach Sentry.
    run = await _create_run()
    lost = "a real fact the model buried in verbosity: " + "x" * CONTENT_HARD_LIMIT

    assert await _extract(run, [ExtractedObservation(category="pitfall", content=lost)], caplog) == []

    (record,) = caplog.records
    assert record.levelname == "ERROR"
    assert "unrecoverably" in record.getMessage()
    assert lost[:100] in record.getMessage()


@pytest.mark.django_db(transaction=True)
async def test_too_short_drop_is_only_a_warning(caplog):
    # An "n/a" carries no fact, so nothing recoverable was lost and this must not page anyone.
    run = await _create_run()

    assert await _extract(run, [ExtractedObservation(category="workflow", content="n/a")], caplog) == []

    (record,) = caplog.records
    assert record.levelname == "WARNING"


@pytest.mark.django_db(transaction=True)
async def test_a_batch_over_the_prompt_guidance_is_kept_whole(caplog):
    # Deliberately uncapped: a dropped observation here is never persisted, so it cannot be
    # re-queued. MAX_OPERATIONS in consolidation is the throttle, and it defers instead.
    run = await _create_run()
    extracted = [
        ExtractedObservation(category="pitfall", content=f"a durable fact about thing {i}")
        for i in range(MAX_OBSERVATIONS + 4)
    ]

    result = await _extract(run, extracted, caplog)

    assert len(result) == MAX_OBSERVATIONS + 4
    assert caplog.records == []


class TestExtractFromTranscript:
    async def test_uses_the_supplied_models_and_never_reads_site_settings(self):
        extracted = [ExtractedObservation(category="build_test", content="`make test` needs -n auto")]

        with (
            patch("memory.extraction.build_structured_llm", return_value=_structured_llm_returning(extracted)) as build,
            patch("memory.extraction.site_settings", _site_settings(memory_extraction_model_name=None)),
        ):
            result = await extract_from_transcript(
                "[human] fix it\n[ai] done", repo_id="eval/x", status="SUCCESSFUL", model_names=["some:model"]
            )

        assert len(result) == 1
        assert build.call_args.args[1] == ["some:model"]

    async def test_falls_back_to_site_settings_when_no_models_given(self):
        with (
            patch("memory.extraction.build_structured_llm", return_value=_structured_llm_returning([])) as build,
            patch("memory.extraction.site_settings", _site_settings()),
        ):
            await extract_from_transcript("[human] hi\n[ai] ok", repo_id="eval/x", status="SUCCESSFUL")

        assert build.call_args.args[1] == ("openrouter:openai/gpt-5.4-mini", "openrouter:anthropic/claude-haiku-4.5")

    async def test_returns_empty_when_no_model_is_configured(self, caplog):
        with (
            caplog.at_level(logging.ERROR, logger="daiv.memory"),
            patch("memory.extraction.build_structured_llm") as build,
            patch(
                "memory.extraction.site_settings",
                _site_settings(memory_extraction_model_name=None, memory_extraction_fallback_model_name=None),
            ),
        ):
            assert await extract_from_transcript("[ai] x", repo_id="eval/x", status="SUCCESSFUL") == []

        build.assert_not_called()
        assert "no extraction model configured" in caplog.text

    async def test_drops_unusable_observations(self):
        extracted = [
            ExtractedObservation(category="build_test", content="short"),
            ExtractedObservation(category="build_test", content="a genuinely usable observation"),
        ]
        with (
            patch("memory.extraction.build_structured_llm", return_value=_structured_llm_returning(extracted)),
            patch("memory.extraction.site_settings", _site_settings()),
        ):
            result = await extract_from_transcript(
                "[ai] x", repo_id="eval/x", status="FAILED", model_names=["some:model"]
            )

        assert len(result) == 1

    async def test_trace_metadata_carries_both_repo_id_and_run_ref(self):
        llm = _structured_llm_returning([])
        with (
            patch("memory.extraction.build_structured_llm", return_value=llm),
            patch("memory.extraction.site_settings", _site_settings()),
        ):
            await extract_from_transcript(
                "[ai] x", repo_id="eval/x", status="SUCCESSFUL", model_names=["some:model"], run_ref="case-7"
            )

        assert llm.with_config.call_args.kwargs["metadata"] == {"repo_id": "eval/x", "run_ref": "case-7"}


@pytest.mark.django_db(transaction=True)
async def test_extract_observations_labels_the_trace_with_the_runs_pk():
    # Regression guard: extract_from_transcript's metadata dict must still carry a run-identifying
    # dimension, or LangSmith traces lose it (it previously read run_id=str(run.pk)).
    run = await _create_run()
    llm = _structured_llm_returning([])

    with (
        patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
        patch("memory.extraction.build_structured_llm", return_value=llm),
        patch("memory.extraction.site_settings", _site_settings()),
    ):
        await extract_observations(run)

    assert llm.with_config.call_args.kwargs["metadata"] == {"repo_id": run.repo_id, "run_ref": str(run.pk)}


def test_usable_labels_its_logs_with_the_supplied_run_ref(caplog):
    from memory.extraction import _usable

    with caplog.at_level(logging.WARNING, logger="daiv.memory"):
        kept = _usable([ExtractedObservation(category="build_test", content="short")], run_ref="case-042")

    assert kept == []
    assert "case-042" in caplog.text


class TestExtractionSeesMemory:
    @pytest.mark.django_db(transaction=True)
    async def test_the_stored_document_reaches_the_prompt(self):
        from memory.models import RepositoryMemory

        run = await _create_run()
        await RepositoryMemory.objects.acreate(repo_id=run.repo_id, content="## Build & test\n- a known fact")
        llm = _structured_llm_returning([])

        with (
            patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
            patch("memory.extraction.build_structured_llm", return_value=llm),
            patch("memory.extraction.site_settings", _site_settings()),
        ):
            await extract_observations(run)

        human = llm.with_config.return_value.ainvoke.call_args.args[0][1]
        assert "- a known fact" in human.content

    @pytest.mark.django_db(transaction=True)
    async def test_a_lookup_failure_falls_open_to_no_memory(self):
        run = await _create_run()
        llm = _structured_llm_returning([])

        with (
            patch("core.checkpointer.open_checkpointer", _checkpointer_with(TRANSCRIPT)),
            patch("memory.extraction.build_structured_llm", return_value=llm),
            patch("memory.extraction.site_settings", _site_settings()),
            patch("memory.models.RepositoryMemory.objects.filter", side_effect=RuntimeError("db down")),
            # side_effect delegates to the real function so the LLM call below still happens;
            # this only lets us assert the exact "" the signature promises, not just its rendering.
            patch("memory.extraction.extract_from_transcript", side_effect=extract_from_transcript) as delegate,
        ):
            assert await extract_observations(run) == []

        assert delegate.call_args.kwargs["memory"] == ""

        human = llm.with_config.return_value.ainvoke.call_args.args[0][1]
        assert "no memory yet" in human.content
