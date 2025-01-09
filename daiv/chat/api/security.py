from ninja.security import APIKeyHeader

from accounts.models import APIKey

API_KEY_HEADER = "X-API-Key"


class APIKeyAuth(APIKeyHeader):
    """
    Authentication class for the API using API keys.
    """

    param_name = API_KEY_HEADER

    def authenticate(self, request, key):
        if key is None:
            return None

        try:
            api_key = APIKey.objects.get_from_key(key)
        except APIKey.DoesNotExist:
            return None
        return api_key.user
