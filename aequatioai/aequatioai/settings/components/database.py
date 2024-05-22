from decouple import Choices, config
from get_docker_secret import get_docker_secret

DATABASES_OPTIONS = {}
DB_ENGINE = config(
    "DB_ENGINE",
    default="django.db.backends.postgresql",
    cast=Choices(
        [
            "django.db.backends.postgresql",
            "django.db.backends.mysql",
            "django.db.backends.sqlite3",
            "django.db.backends.oracle",
        ]
    ),
)

if DB_ENGINE == "django.db.backends.postgresql":
    DATABASES_OPTIONS = {
        "sslmode": config(
            "DB_SSLMODE",
            default="require",
            cast=Choices(["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]),
        )
    }

DATABASES = {
    "default": {
        "ENGINE": DB_ENGINE,
        "NAME": config("DB_NAME"),
        "USER": config("DB_USER"),
        "PASSWORD": get_docker_secret("DB_PASSWORD", safe=False),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default=5432, cast=int),
        "CONN_MAX_AGE": config("DB_CONN_MAX_AGE", default=0, cast=int),
        "OPTIONS": DATABASES_OPTIONS,
    }
}
