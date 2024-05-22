from get_docker_secret import get_docker_secret

DJANGO_REDIS_URL = get_docker_secret("DJANGO_REDIS_URL")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": DJANGO_REDIS_URL,
        "KEY_PREFIX": "aequatioai",
    }
}
