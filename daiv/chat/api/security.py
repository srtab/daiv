from typing import TYPE_CHECKING, Any

from ninja.security import HttpBearer

from accounts.models import APIKey, User

if TYPE_CHECKING:
    from collections.abc import Coroutine


class AuthBearer(HttpBearer):
    """
    Authentication class for the API using API keys.
    """

    async def authenticate(self, request, key: str | None) -> Coroutine[Any, Any, User | None]:
        if key is None:
            return None

        api_key = await APIKey.objects.get_active_key(key)
        return api_key.user if api_key else None
