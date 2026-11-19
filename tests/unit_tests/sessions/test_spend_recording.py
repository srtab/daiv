import asyncio
import uuid
from decimal import Decimal
from unittest import mock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from sessions.models import Run, RunStatus, Session, SessionOrigin, SpendRecord, SpendTask
from sessions.spend_recording import BackgroundSpend, RunSpendRecorder

from automation.agent.usage_tracking import track_usage_metadata

pytestmark = pytest.mark.django_db(transaction=True)


def _message(model="claude-sonnet-4-6", input_tokens=100, output_tokens=10) -> AIMessage:
    return AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        response_metadata={"model_name": model},
    )


def _model(*messages: AIMessage) -> GenericFakeChatModel:
    return GenericFakeChatModel(
        messages=iter(messages), metadata={"daiv_provider_slug": "anthropic", "daiv_provider_name": "Anthropic"}
    )


def _result(message: AIMessage) -> LLMResult:
    return LLMResult(generations=[[ChatGeneration(message=message)]])


@pytest.fixture
def run(member_user):
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="g/r")
    return Run.objects.create(
        session=session, trigger_type=SessionOrigin.CHAT, repo_id="g/r", status=RunStatus.RUNNING, user=member_user
    )


async def _until(predicate, timeout: float = 5.0):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.01)


async def _recorded(run_id) -> list[SpendRecord]:
    return [record async for record in SpendRecord.objects.filter(run_id=run_id).order_by("model")]


class TestRunSpendRecorder:
    async def test_records_follow_each_call_and_fill_the_run_before_it_ends(self, run, member_user):
        with track_usage_metadata() as handler:
            async with RunSpendRecorder(str(run.pk), handler):
                model = _model(_message(input_tokens=100), _message(input_tokens=300))
                await model.ainvoke("a")
                await model.ainvoke("b")

                async def _both_written():
                    row = await Run.objects.aget(pk=run.pk)
                    return row.input_tokens == 400

                await _until(_both_written)
                ledger, _ = handler.snapshot()
                [record] = await _recorded(run.pk)

        entry = ledger[("anthropic", "claude-sonnet-4-6")]
        assert (record.provider_slug, record.provider_name) == ("anthropic", "Anthropic")
        assert (record.input_tokens, record.total_tokens) == (entry.input_tokens, entry.total_tokens)
        assert record.cost_usd == entry.cost.quantize(Decimal("0.000001"))
        assert (record.user_id, record.person_name, record.trigger_type) == (
            member_user.pk,
            str(member_user),
            SessionOrigin.CHAT,
        )
        assert (record.session_id, record.repo_id, record.occurred_at) == (run.session_id, "g/r", run.created_at)

    async def test_a_burst_of_calls_coalesces_into_one_write(self, run):
        with track_usage_metadata() as handler:
            recorder = RunSpendRecorder(str(run.pk), handler)
            with mock.patch.object(
                RunSpendRecorder, "_save", autospec=True, side_effect=RunSpendRecorder._save
            ) as save:
                async with recorder:
                    for _ in range(5):
                        handler.on_llm_end(_result(_message()))

                    async def _written():
                        return save.call_count == 1

                    await _until(_written)
                    await asyncio.sleep(0.05)
                    assert save.call_count == 1

        [record] = await _recorded(run.pk)
        assert record.input_tokens == 500

    async def test_a_block_that_raises_keeps_the_spend_of_its_calls(self, run):
        with track_usage_metadata() as handler, pytest.raises(RuntimeError):
            async with RunSpendRecorder(str(run.pk), handler):
                handler.on_update = None
                handler.on_llm_end(_result(_message()))
                raise RuntimeError("agent crashed")

        [record] = await _recorded(run.pk)
        assert record.input_tokens == 100
        assert (await Run.objects.aget(pk=run.pk)).total_tokens == 110

    async def test_a_cancelled_block_still_runs_the_final_write(self, run):
        started = asyncio.Event()

        async def _agent():
            with track_usage_metadata() as handler:
                async with RunSpendRecorder(str(run.pk), handler):
                    handler.on_update = None
                    handler.on_llm_end(_result(_message()))
                    started.set()
                    await asyncio.Event().wait()

        task = asyncio.create_task(_agent())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        [record] = await _recorded(run.pk)
        assert record.input_tokens == 100

    async def test_a_failing_write_is_logged_and_the_run_goes_on(self, run, caplog):
        with track_usage_metadata() as handler:
            with mock.patch.object(RunSpendRecorder, "_save", side_effect=RuntimeError("db down")):
                async with RunSpendRecorder(str(run.pk), handler):
                    handler.on_llm_end(_result(_message()))
            assert handler.ledger

        levels = {r.levelname for r in caplog.records if str(run.pk) in r.getMessage()}
        assert levels == {"ERROR"}
        assert await _recorded(run.pk) == []

    async def test_an_unpriced_call_clears_the_run_cost(self, run):
        with track_usage_metadata() as handler:
            async with RunSpendRecorder(str(run.pk), handler):
                handler.on_update = None
                handler.on_llm_end(_result(_message()))
                await handler_write(handler, run)
                handler.on_llm_end(_result(_message(model="acme/mystery-1")))

        row = await Run.objects.aget(pk=run.pk)
        assert row.cost_usd is None
        assert len(await _recorded(run.pk)) == 2

    async def test_a_missing_run_records_nothing(self, caplog):
        with track_usage_metadata() as handler:
            async with RunSpendRecorder(str(uuid.uuid4()), handler):
                handler.on_llm_end(_result(_message()))

        assert await SpendRecord.objects.acount() == 0
        assert handler.on_update is None


async def handler_write(handler, run) -> None:
    """Write what the handler holds now, as the writer would after one call."""
    recorder = RunSpendRecorder(str(run.pk), handler)
    recorder._shared = await recorder._read_run()
    await recorder._write()
    assert (await Run.objects.aget(pk=run.pk)).cost_usd is not None


def _tagged(provider: str, *messages: AIMessage) -> GenericFakeChatModel:
    return GenericFakeChatModel(
        messages=iter(messages), metadata={"daiv_provider_slug": provider, "daiv_provider_name": provider.title()}
    )


class TestBackgroundSpend:
    def test_one_record_per_provider_and_model_for_the_session_it_served(self, run, member_user):
        with BackgroundSpend(SpendTask.TITLE, session=run.session, repo_id="g/r"):
            _tagged("anthropic", _message(model="a"), _message(model="b")).batch(["x", "y"])
            _tagged("openrouter", _message(model="a")).invoke("z")

        records = list(SpendRecord.objects.order_by("provider_slug", "model"))
        assert [(r.provider_slug, r.model) for r in records] == [
            ("anthropic", "a"),
            ("anthropic", "b"),
            ("openrouter", "a"),
        ]
        assert {(r.task, r.trigger_type, r.session_id, r.repo_id, r.run_id) for r in records} == {
            (SpendTask.TITLE, "", run.session_id, "g/r", None)
        }

    async def test_a_run_gives_its_session_repository_and_person(self, run, member_user):
        async with BackgroundSpend(SpendTask.RUN_CLASSIFICATION, run=run):
            await _tagged("anthropic", _message()).ainvoke("x")

        record = await SpendRecord.objects.aget()
        assert (record.run_id, record.session_id, record.repo_id) == (run.pk, run.session_id, "g/r")
        assert (record.user_id, record.person_name) == (member_user.pk, str(member_user))

    def test_repository_work_belongs_to_daiv(self):
        with BackgroundSpend(SpendTask.MEMORY_CONSOLIDATION, repo_id="g/r"):
            _tagged("anthropic", _message()).invoke("x")

        record = SpendRecord.objects.get()
        assert (record.user_id, record.external_uid, record.person_name, record.session_id) == (None, "", "", None)

    def test_a_block_that_raises_still_records_its_calls(self):
        with pytest.raises(RuntimeError, match="parse"), BackgroundSpend(SpendTask.MEMORY_CONSOLIDATION, repo_id="g/r"):
            _tagged("anthropic", _message()).invoke("x")
            raise RuntimeError("parse failed")

        assert SpendRecord.objects.count() == 1

    async def test_a_failing_write_is_logged_and_the_blocks_own_error_still_raises(self, caplog):
        with (
            mock.patch.object(BackgroundSpend, "_save", side_effect=RuntimeError("db down")),
            pytest.raises(ValueError, match="own"),
        ):
            async with BackgroundSpend(SpendTask.MEMORY_CONSOLIDATION, repo_id="g/r"):
                await _tagged("anthropic", _message()).ainvoke("x")
                raise ValueError("own error")

        assert [r.levelname for r in caplog.records if "memory_consolidation spend" in r.getMessage()] == ["ERROR"]

    def test_a_block_with_no_calls_writes_nothing(self):
        with BackgroundSpend(SpendTask.TITLE):
            pass

        assert SpendRecord.objects.count() == 0
