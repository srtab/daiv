from __future__ import annotations

from typing import Any

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from automation.agent.model_catalog.cache_keys import MODEL_CATALOG_CACHE_KEY_FMT
from core.models import Provider


@receiver([post_save, post_delete], sender=Provider, dispatch_uid="automation.clear_model_catalog")
def clear_model_catalog(sender: type[Provider], instance: Provider, **kwargs: Any) -> None:
    """Drop the provider's cached model list once the write commits, so the agent picker refetches it."""
    key = MODEL_CATALOG_CACHE_KEY_FMT.format(slug=instance.slug)
    transaction.on_commit(lambda: cache.delete(key))
