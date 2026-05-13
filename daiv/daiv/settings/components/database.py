from urllib.parse import urlencode

from decouple import Choices, config
from get_docker_secret import get_docker_secret

DATABASES_OPTIONS = {
    "sslmode": config(
        "DB_SSLMODE",
        default="require",
        cast=Choices(["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]),
    ),
    "pool": {"max_size": config("DB_POOL_MAX_SIZE", default=15, cast=int)},
    # Probe idle connections so middleboxes (e.g. Docker Swarm IPVS, default 15min)
    # don't silently drop pooled connections, causing a reconnect on the next request.
    "keepalives": 1,
    "keepalives_idle": config("DB_KEEPALIVES_IDLE", default=60, cast=int),
    "keepalives_interval": config("DB_KEEPALIVES_INTERVAL", default=10, cast=int),
    "keepalives_count": config("DB_KEEPALIVES_COUNT", default=5, cast=int),
}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME"),
        "USER": config("DB_USER"),
        "PASSWORD": get_docker_secret("DB_PASSWORD", safe=False),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default=5432, cast=int),
        "OPTIONS": DATABASES_OPTIONS,
        "CONN_HEALTH_CHECKS": True,
    }
}

query_params = {"sslmode": DATABASES_OPTIONS["sslmode"]}

DB_URI = "postgresql://{USER}:{PASSWORD}@{HOST}:{PORT}/{NAME}?{encoded_query}".format(
    encoded_query=urlencode(query_params), **DATABASES["default"]
)
