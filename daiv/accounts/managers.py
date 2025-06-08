from __future__ import annotations

from typing import TYPE_CHECKING, Generic, TypeVar

from django.db import models
from django.utils import timezone

from accounts.crypto import KeyGenerator

if TYPE_CHECKING:
    from datetime import datetime

    from accounts.models import APIKey, User

T = TypeVar("T", bound="APIKey")


class APIKeyManager(models.Manager, Generic[T]):  # noqa: UP046
    """
    Manager for the APIKey model.
    """

    key_generator = KeyGenerator()

    async def create_key(self, user: User, name: str, expires_at: datetime | None = None) -> tuple[T, str]:
        key, prefix, hashed_key = self.key_generator.generate()
        obj = await self.acreate(user=user, name=name, expires_at=expires_at, prefix=prefix, hashed_key=hashed_key)
        return obj, key

    async def get_from_key(self, key: str) -> T:
        prefix, _, _ = key.partition(".")

        api_key = await self.get_usable_keys().select_related("user").aget(prefix=prefix)

        if not self.key_generator.verify(key, api_key.hashed_key):
            raise self.model.DoesNotExist("Key is not valid.")
        return api_key

    def get_usable_keys(self) -> models.QuerySet:
        return self.filter(
            models.Q(revoked=False), models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=timezone.now())
        )
