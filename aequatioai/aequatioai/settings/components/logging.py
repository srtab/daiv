from decouple import Choices, config

LOGGING_LEVEL = config(
    "DJANGO_LOGGING_LEVEL", default="INFO", cast=Choices(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
)


LOGGING: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[%(asctime)s] %(levelname)s - %(name)s - %(message)s", "datefmt": "%d-%m-%Y:%H:%M:%S %z"}
    },
    "handlers": {"console": {"level": "DEBUG", "class": "logging.StreamHandler", "formatter": "verbose"}},
    "loggers": {
        "": {"level": LOGGING_LEVEL, "handlers": ["console"]},
        "automation": {"level": "DEBUG", "handlers": ["console"], "propagate": False},
    },
}
