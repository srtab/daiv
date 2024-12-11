from django.core.exceptions import DisallowedHost

from decouple import config
from get_docker_secret import get_docker_secret

from daiv import __version__
from daiv.settings.components import ENVIRONMENT

SENTRY_DSN = get_docker_secret("SENTRY_DSN")
SENTRY_DEBUG = config("SENTRY_DEBUG", cast=bool, default=False)
SENTRY_ENABLE_TRACING = config("SENTRY_ENABLE_TRACING", cast=bool, default=False)
SENTRY_CA_CERTS = config("SENTRY_CA_CERTS", cast=str, default=None)

if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.redis import RedisIntegration

    sentry_sdk.init(
        ignore_errors=[DisallowedHost, KeyboardInterrupt],
        dsn=SENTRY_DSN,
        release=__version__,
        environment=ENVIRONMENT,
        debug=SENTRY_DEBUG,
        enable_tracing=SENTRY_ENABLE_TRACING,
        profiles_sample_rate=1.0 if SENTRY_ENABLE_TRACING else 0.0,
        server_name=config("NODE_HOSTNAME", default=None),
        ca_certs=SENTRY_CA_CERTS,
        integrations=[
            DjangoIntegration(),
            LoggingIntegration(),
            RedisIntegration(),
            CeleryIntegration(monitor_beat_tasks=True),
        ],
    )

    if SERVICE_NAME := config("SERVICE_NAME", default=None):
        sentry_sdk.set_tag("service_name", SERVICE_NAME)
