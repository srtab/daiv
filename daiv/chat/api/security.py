from asgiref.sync import sync_to_async
from ninja.security import HttpBearer

from accounts.models import APIKey, User


class AuthBearer(HttpBearer):
    """
    Authentication class for the API using API keys.
    """

    def authenticate(self, request, key: str | None) -> User | None:
        if key is None:
            return None

        try:
            api_key = APIKey.objects.get_from_key(key)
        except APIKey.DoesNotExist:
            return None

        return api_key.user


class AsyncAuthBearer(AuthBearer):
    """Async authentication class for the API using API keys."""

    async def authenticate(self, request, key: str | None) -> User | None:
        return await sync_to_async(super().authenticate)(request, key)
