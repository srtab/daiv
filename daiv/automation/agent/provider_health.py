from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from langchain_core.callbacks import BaseCallbackHandler

from automation.agent.provider_errors import classify_provider_error, tag_provider_failure
from core.models import PROVIDER_FAILURE_TTL, Provider, ProviderFailureKind, _close_old_connections_outside_transactions
from core.provider_failures import ADMIN_KINDS, ProviderFailure

if TYPE_CHECKING:
    from datetime import datetime

    from langchain_core.outputs import LLMResult

logger = logging.getLogger("daiv.automation")


class ProviderHealthCallback(BaseCallbackHandler):
    """Tags a provider error with its failure kind, and keeps the provider row's health mark in step."""

    # Not inline: an async run awaits this sync handler in an executor thread, so the tag lands before the
    # error propagates and the ORM never runs on the event loop.
    run_inline = False
    # Unhandled, a chat-model start makes langchain stringify the whole prompt on every call.
    ignore_chat_model = True

    def __init__(self, provider_slug: str, model_name: str) -> None:
        self.provider_slug = provider_slug
        self.model_name = model_name

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        kind = classify_provider_error(error)
        if kind is None:
            return
        tag_provider_failure(error, ProviderFailure(kind, self.provider_slug))
        if kind in ADMIN_KINDS:
            mark_provider_failure(self.provider_slug, kind, self.model_name)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        clear_provider_failure(self.provider_slug, self.model_name)


def mark_provider_failure(provider_slug: str, kind: str, model_name: str, now: datetime | None = None) -> None:
    now = now or timezone.now()
    try:
        _close_old_connections_outside_transactions()
        provider = Provider.objects.filter(slug=provider_slug)
        repeat = provider.filter(
            failure_kind=kind, failure_model=model_name, last_failed_at__gte=now - PROVIDER_FAILURE_TTL
        )
        if not repeat.update(last_failed_at=now):
            provider.update(failure_kind=kind, failure_model=model_name, failing_since=now, last_failed_at=now)
        Provider.invalidate_cache()
    except Exception:
        logger.exception("Could not mark provider %r as failing with %s", provider_slug, kind)
    finally:
        with contextlib.suppress(Exception):
            _close_old_connections_outside_transactions()


def clear_provider_failure(provider_slug: str, model_name: str) -> None:
    try:
        row = Provider.get_cached_by_slug().get(provider_slug)
        if row is None:
            return
        kind = row.active_failure_kind(timezone.now())
        if not kind or (kind != ProviderFailureKind.AUTH_FAILED and row.failure_model != model_name):
            return
        _close_old_connections_outside_transactions()
        Provider.objects.filter(slug=provider_slug, failure_kind=kind).update(
            failure_kind="", failure_model="", failing_since=None, last_failed_at=None
        )
        Provider.invalidate_cache()
    except Exception:
        logger.exception("Could not clear the failure mark on provider %r", provider_slug)
    finally:
        with contextlib.suppress(Exception):
            _close_old_connections_outside_transactions()
