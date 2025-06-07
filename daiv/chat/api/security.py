from collections.abc import Coroutine
from typing import Any

from ninja.security import HttpBearer

from accounts.models import APIKey, User


class AuthBearer(HttpBearer):
    """
    Authentication class for the API using API keys.
    """

    async def authenticate(self, request, key: str | None) -> Coroutine[Any, Any, User | None]:
        if key is None:
            return None

        try:
            api_key = await APIKey.objects.get_from_key(key)
        except APIKey.DoesNotExist:
            return None

        return api_key.user
