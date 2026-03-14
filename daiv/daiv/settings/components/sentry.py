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

_HEALTH_CHECK_PATHS = ("/-/alive/",)


def _traces_sampler(sampling_context: dict) -> float:
    """Return 0.0 for health check requests to avoid sending noise to Sentry."""
    asgi_scope = sampling_context.get("asgi_scope", {})
    path = asgi_scope.get("path") or sampling_context.get("wsgi_environ", {}).get("PATH_INFO", "")
    if path.startswith(_HEALTH_CHECK_PATHS):
        return 0.0
    return SENTRY_TRACES_SAMPLE_RATE


if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.anthropic import AnthropicIntegration
    from sentry_sdk.integrations.google_genai import GoogleGenAIIntegration
    from sentry_sdk.integrations.langchain import LangchainIntegration
    from sentry_sdk.integrations.langgraph import LanggraphIntegration
    from sentry_sdk.integrations.openai import OpenAIIntegration

    sentry_sdk.init(
        ignore_errors=[DisallowedHost, KeyboardInterrupt],
        integrations=[
            AnthropicIntegration(include_prompts=SENTRY_SEND_DEFAULT_PII),
            GoogleGenAIIntegration(include_prompts=SENTRY_SEND_DEFAULT_PII),
            LangchainIntegration(include_prompts=SENTRY_SEND_DEFAULT_PII),
            LanggraphIntegration(include_prompts=SENTRY_SEND_DEFAULT_PII),
            OpenAIIntegration(include_prompts=SENTRY_SEND_DEFAULT_PII),
        ],
        dsn=SENTRY_DSN,
        release=__version__,
        environment=ENVIRONMENT,
        debug=SENTRY_DEBUG,
        enable_logs=SENTRY_ENABLE_LOGS,
        traces_sampler=_traces_sampler,
        profiles_sample_rate=SENTRY_PROFILES_SAMPLE_RATE,
        send_default_pii=SENTRY_SEND_DEFAULT_PII,
        server_name=config("NODE_HOSTNAME", default=None),
    )

    if SERVICE_NAME := config("SERVICE_NAME", default=None):
        sentry_sdk.set_tag("service_name", SERVICE_NAME)
