from decouple import config
from get_docker_secret import get_docker_secret

DJANGO_REDIS_URL = get_docker_secret("DJANGO_REDIS_URL")
DJANGO_REDIS_SESSION_URL = get_docker_secret("DJANGO_REDIS_SESSION_URL", default=DJANGO_REDIS_URL)
DJANGO_REDIS_CHECKPOINT_URL = get_docker_secret("DJANGO_REDIS_CHECKPOINT_URL", default=DJANGO_REDIS_URL)
DJANGO_REDIS_CHECKPOINT_TTL_MINUTES = config("DJANGO_REDIS_CHECKPOINT_TTL_MINUTES", default=60 * 24 * 7, cast=int)

_REDIS_OPTIONS = {
    "socket_connect_timeout": 5,
    "socket_timeout": 5,
    "pool_class": "redis.connection.BlockingConnectionPool",
    "max_connections": 50,
}

CACHES = {
    "default": {
        "BACKEND": "core.cache.RedisCache",
        "LOCATION": DJANGO_REDIS_URL,
        "KEY_PREFIX": "daiv",
        "TIMEOUT": 300,
        "OPTIONS": _REDIS_OPTIONS,
    },
    "sessions": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": DJANGO_REDIS_SESSION_URL,
        "KEY_PREFIX": "daiv:sessions",
        "TIMEOUT": None,
        "OPTIONS": _REDIS_OPTIONS,
    },
}
