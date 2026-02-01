from django.core.exceptions import DisallowedHost

from decouple import config
from get_docker_secret import get_docker_secret

from daiv import __version__
from daiv.settings.components import ENVIRONMENT

SENTRY_DSN = get_docker_secret("SENTRY_DSN")
SENTRY_DEBUG = config("SENTRY_DEBUG", cast=bool, default=False)
SENTRY_ENABLE_LOGS = config("SENTRY_ENABLE_LOGS", cast=bool, default=False)
SENTRY_TRACES_SAMPLE_RATE = config("SENTRY_TRACES_SAMPLE_RATE", cast=float, default=0.0)
SENTRY_PROFILES_SAMPLE_RATE = config("SENTRY_PROFILES_SAMPLE_RATE", cast=float, default=0.0)
SENTRY_SEND_DEFAULT_PII = config("SENTRY_SEND_DEFAULT_PII", cast=bool, default=False)

if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        ignore_errors=[DisallowedHost, KeyboardInterrupt],
        dsn=SENTRY_DSN,
        release=__version__,
        environment=ENVIRONMENT,
        debug=SENTRY_DEBUG,
        enable_logs=SENTRY_ENABLE_LOGS,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        profiles_sample_rate=SENTRY_PROFILES_SAMPLE_RATE,
        send_default_pii=SENTRY_SEND_DEFAULT_PII,
        server_name=config("NODE_HOSTNAME", default=None),
    )

    if SERVICE_NAME := config("SERVICE_NAME", default=None):
        sentry_sdk.set_tag("service_name", SERVICE_NAME)
