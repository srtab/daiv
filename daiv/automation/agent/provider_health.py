from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from langchain_core.callbacks import BaseCallbackHandler

from automation.agent.provider_errors import classify_provider_error, tag_provider_failure
from core.models import PROVIDER_FAILURE_TTL, Provider, ProviderFailureKind, close_old_connections_outside_transactions
from core.provider_failures import ADMIN_KINDS, ProviderFailure

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from datetime import datetime

    from langchain_core.outputs import LLMResult

logger = logging.getLogger("daiv.automation")

# One worker applies marks and clears in the order their calls finished.
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider-health")


class ProviderHealthCallback(BaseCallbackHandler):
    """Tags a provider error with its failure kind, and keeps the provider row's health mark in step."""

    # Inline, so the tag lands before the error propagates and a streamed token costs no executor hop.
    run_inline = True
    # Unhandled, a chat-model start makes langchain stringify the whole prompt on every call.
    ignore_chat_model = True

    def __init__(self, provider_slug: str, model_name: str) -> None:
        self.provider_slug = provider_slug
        self.model_name = model_name

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        # A cancellation raised while handling a provider error would otherwise inherit its kind via __context__.
        if not isinstance(error, Exception):
            return
        try:
            kind = classify_provider_error(error)
            if kind is not None:
                tag_provider_failure(error, ProviderFailure(kind, self.provider_slug))
        except Exception:
            logger.exception("Could not classify a model error from provider %r", self.provider_slug)
            return
        if kind in ADMIN_KINDS:
            _off_the_event_loop(mark_provider_failure, self.provider_slug, kind, self.model_name)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        _off_the_event_loop(clear_provider_failure, self.provider_slug, self.model_name)


def _off_the_event_loop(job: Callable[..., None], *args: Any) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        job(*args)
    else:
        _executor.submit(job, *args)


@contextlib.contextmanager
def _logged_db_failure(message: str, *args: Any) -> Generator[None]:
    try:
        close_old_connections_outside_transactions()
        yield
    except Exception:
        logger.exception(message, *args)
    finally:
        with contextlib.suppress(Exception):
            close_old_connections_outside_transactions()


def mark_provider_failure(provider_slug: str, kind: str, model_name: str, now: datetime | None = None) -> None:
    now = now or timezone.now()
    with _logged_db_failure("Could not mark provider %r as failing with %s", provider_slug, kind):
        provider = Provider.objects.filter(slug=provider_slug)
        with transaction.atomic():
            repeat = provider.filter(failure_kind=kind, last_failed_at__gte=now - PROVIDER_FAILURE_TTL)
            if repeat.update(last_failed_at=now):
                return
            provider.update(failure_kind=kind, failure_model=model_name, failing_since=now, last_failed_at=now)
        Provider.invalidate_cache()


def clear_provider_failure(provider_slug: str, model_name: str) -> None:
    with _logged_db_failure("Could not clear the failure mark on provider %r", provider_slug):
        row = Provider.get_cached_by_slug().get(provider_slug)
        if row is None:
            return
        kind = row.active_failure_kind(timezone.now())
        if kind == ProviderFailureKind.AUTH_FAILED:
            marked = Provider.objects.filter(slug=provider_slug, failure_kind=kind)
        elif kind and row.failure_model == model_name:
            marked = Provider.objects.filter(slug=provider_slug, failure_kind=kind, failure_model=model_name)
        else:
            return
        with transaction.atomic():
            cleared = marked.update(failure_kind="", failure_model="", failing_since=None, last_failed_at=None)
        if cleared:
            Provider.invalidate_cache()
