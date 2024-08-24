from get_docker_secret import get_docker_secret

DJANGO_REDIS_URL = get_docker_secret("DJANGO_REDIS_URL")

CACHES = {"default": {"BACKEND": "core.cache.RedisCache", "LOCATION": DJANGO_REDIS_URL, "KEY_PREFIX": "daiv"}}
