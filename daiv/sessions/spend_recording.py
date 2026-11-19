"""Writing ``SpendRecord`` rows: a run's as each model call finishes, and background work's when it ends.

``RunSpendRecorder`` never writes from the callback thread LangChain runs the usage handler on. The
handler's ``on_update`` only wakes one writer task on the run's event loop, which snapshots the handler
and writes absolute totals, so writes are idempotent, bursts of calls coalesce into one write, and a
killed worker loses only the calls that finished after the last completed write. A failed write is
logged and never fails the work it measures.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from asgiref.sync import sync_to_async

from sessions.models import Run, Session, SpendRecord, SpendTask, usage_field_updates

if TYPE_CHECKING:
    from types import TracebackType

    from automation.agent.usage_tracking import CostAwareUsageMetadataCallbackHandler, LedgerEntry, UsageSummary

logger = logging.getLogger("daiv.sessions")


class RunSpendRecorder:
    """Keeps a run's ``SpendRecord`` rows and ``Run`` usage fields in step with its usage handler.

    Use as ``async with RunSpendRecorder(run_id, handler):`` inside the handler's ``track_usage_metadata``
    block. Every exit path, cancellation included, ends with one final shielded write.
    """

    def __init__(self, run_id: str, handler: CostAwareUsageMetadataCallbackHandler) -> None:
        self.run_id = run_id
        self.handler = handler
        self._changed = asyncio.Event()
        self._stopping = False
        self._writer: asyncio.Task | None = None
        self._shared: dict[str, Any] = {}
        self._written: dict[tuple[str, str], LedgerEntry] = {}

    async def __aenter__(self) -> RunSpendRecorder:
        try:
            self._shared = await self._read_run() or {}
        except Exception:
            logger.exception("Could not read run_id=%s to record its spend", self.run_id)
        if not self._shared:
            return self
        loop = asyncio.get_running_loop()

        def wake() -> None:
            loop.call_soon_threadsafe(self._changed.set)

        self.handler.on_update = wake
        self._writer = asyncio.create_task(self._write_on_change())
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._writer is None:
            return
        self.handler.on_update = None
        self._stopping = True
        self._changed.set()
        await asyncio.shield(self._writer)

    async def _write_on_change(self) -> None:
        while not self._stopping or self._changed.is_set():
            await self._changed.wait()
            self._changed.clear()
            await self._write()

    async def _write(self) -> None:
        try:
            ledger, summary = self.handler.snapshot()
            changed = {key: entry for key, entry in ledger.items() if self._written.get(key) != entry}
            if changed:
                await sync_to_async(self._save)(changed, summary)
                self._written.update(changed)
        except Exception:
            logger.exception("Could not record the spend of run_id=%s", self.run_id)

    async def _read_run(self) -> dict[str, Any] | None:
        run = await Run.objects.filter(pk=self.run_id).afirst()
        if run is None:
            logger.error("No Run row %s to record its spend on", self.run_id)
            return None
        return {
            "occurred_at": run.created_at,
            "session_id": run.session_id,
            "repo_id": run.repo_id,
            "trigger_type": run.trigger_type,
            **SpendRecord.person_fields(run),
        }

    def _save(self, changed: dict[tuple[str, str], LedgerEntry], summary: UsageSummary) -> None:
        """Write the ledger entries that ``changed`` since the last write, and the run's totals."""
        run_fields = usage_field_updates(summary.to_dict(), run_ref=self.run_id)
        run_fields.setdefault("cost_usd", None)
        now = timezone.now()
        with transaction.atomic():
            for (provider_slug, model), entry in changed.items():
                key = {"run_id": self.run_id, "provider_slug": provider_slug, "model": model, "task": ""}
                fields = SpendRecord.entry_fields(entry)
                if not SpendRecord.objects.filter(**key).update(**fields, updated_at=now):
                    counts_run = not SpendRecord.objects.filter(run_id=self.run_id, task="").exists()
                    SpendRecord.objects.create(**key, **self._shared, **fields, counts_run=counts_run)
            Run.objects.filter(pk=self.run_id).update(**run_fields)


class BackgroundSpend:
    """Records the model calls DAIV makes outside a run, for the run, session or repository they served.

    Use as ``with`` or ``async with`` around the call. On exit, by any path, it inserts one record per provider and
    model the calls used; a retried task is charged again. ``run`` or ``session`` gives the record its session,
    repository and person; ``person`` (a ``Run`` or ``Session``) overrides the person, and with neither the work
    belongs to DAIV.
    """

    def __init__(
        self,
        task: SpendTask,
        *,
        run: Run | None = None,
        session: Session | None = None,
        repo_id: str | None = None,
        person: Run | Session | None = None,
    ) -> None:
        self.task = task
        self.run = run
        self.session = session
        self.repo_id = repo_id
        self.person = person
        self._stack = ExitStack()
        self.handler: CostAwareUsageMetadataCallbackHandler | None = None

    def __enter__(self) -> BackgroundSpend:
        # Deferred so importing this module never loads the agent stack.
        from automation.agent.usage_tracking import track_usage_metadata

        self.handler = self._stack.enter_context(track_usage_metadata())
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._stack.close()
        self._write()

    async def __aenter__(self) -> BackgroundSpend:
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._stack.close()
        await sync_to_async(self._write)()

    def _write(self) -> None:
        if self.handler is None:
            return
        try:
            ledger, _summary = self.handler.snapshot()
            if ledger:
                self._save(ledger)
        except Exception:
            logger.exception(
                "Could not record the %s spend of run=%s session=%s",
                self.task,
                self.run and self.run.pk,
                self.session and self.session.pk,
            )

    def _save(self, ledger: dict[tuple[str, str], LedgerEntry]) -> None:
        shared = {"run_id": None, "session_id": None, "repo_id": "", **SpendRecord.NO_PERSON}
        if (session := self.session) is not None:
            shared.update(session_id=session.pk, repo_id=session.repo_id, **SpendRecord.person_fields(session))
        if (run := self.run) is not None:
            shared.update(
                run_id=run.pk, session_id=run.session_id, repo_id=run.repo_id, **SpendRecord.person_fields(run)
            )
        if self.person is not None:
            shared.update(SpendRecord.person_fields(self.person))
        if self.repo_id is not None:
            shared["repo_id"] = self.repo_id
        now = timezone.now()
        SpendRecord.objects.bulk_create([
            SpendRecord(
                occurred_at=now,
                task=self.task,
                trigger_type="",
                model=model,
                provider_slug=provider_slug,
                **shared,
                **SpendRecord.entry_fields(entry),
            )
            for (provider_slug, model), entry in ledger.items()
        ])
