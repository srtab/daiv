import re
from typing import Any

from django.core.exceptions import DisallowedHost

from decouple import config
from get_docker_secret import get_docker_secret

from daiv import RELEASE
from daiv.settings.components import ENVIRONMENT

SENTRY_DSN = get_docker_secret("SENTRY_DSN")
SENTRY_DEBUG = config("SENTRY_DEBUG", cast=bool, default=False)
SENTRY_ENABLE_LOGS = config("SENTRY_ENABLE_LOGS", cast=bool, default=False)
SENTRY_TRACES_SAMPLE_RATE = config("SENTRY_TRACES_SAMPLE_RATE", cast=float, default=0.0)
SENTRY_PROFILES_SAMPLE_RATE = config("SENTRY_PROFILES_SAMPLE_RATE", cast=float, default=0.0)
SENTRY_SEND_DEFAULT_PII = config("SENTRY_SEND_DEFAULT_PII", cast=bool, default=False)

_HEALTH_CHECK_PATHS = ("/-/alive/", "/-/version/")


# The Telegram Bot API takes the bot token in the URL *path*, which ``sentry_sdk.utils.parse_url``
# does not sanitize (it strips only userinfo and query values). Every httpx breadcrumb and span
# would otherwise carry a full-control credential.
_TELEGRAM_TOKEN_RE = re.compile(r"(api\.telegram\.org/bot)[^/\s'\"]+")
_MAX_SCRUB_DEPTH = 12


def scrub_telegram_token(text: str) -> str:
    """Replace the bot token in any ``api.telegram.org/bot<TOKEN>/…`` URL with a placeholder."""
    return _TELEGRAM_TOKEN_RE.sub(r"\1[REDACTED]", text)


def scrub_payload(value: Any, _depth: int = 0) -> Any:
    """Apply ``scrub_telegram_token`` to every string reachable in a Sentry payload.

    Walked generically rather than by key path: the URL shows up as a breadcrumb ``data.url``, a
    span ``description``/``data.url`` and — when a traceback keeps an httpx frame — a frame local.
    Anything that is not a string or a container is returned untouched, and the depth bound keeps
    a self-referential frame local from recursing forever.
    """
    if isinstance(value, str):
        return scrub_telegram_token(value)
    if _depth >= _MAX_SCRUB_DEPTH:
        return value
    if isinstance(value, dict):
        return {key: scrub_payload(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_payload(item, _depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_payload(item, _depth + 1) for item in value)
    return value


def _traces_sampler(sampling_context: dict) -> float:
    """Return 0.0 for health check requests to avoid sending noise to Sentry."""
    asgi_scope = sampling_context.get("asgi_scope", {})
    path = asgi_scope.get("path") or sampling_context.get("wsgi_environ", {}).get("PATH_INFO", "")
    if path.startswith(_HEALTH_CHECK_PATHS):
        return 0.0
    return SENTRY_TRACES_SAMPLE_RATE


def _before_breadcrumb(crumb: Any, hint: Any) -> Any:
    return scrub_payload(crumb)


def _before_send(event: Any, hint: Any) -> Any:
    return scrub_payload(event)


def _before_send_transaction(event: Any, hint: Any) -> Any:
    return scrub_payload(event)


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
        release=RELEASE,
        environment=ENVIRONMENT,
        debug=SENTRY_DEBUG,
        enable_logs=SENTRY_ENABLE_LOGS,
        traces_sampler=_traces_sampler,
        before_breadcrumb=_before_breadcrumb,
        before_send=_before_send,
        before_send_transaction=_before_send_transaction,
        profiles_sample_rate=SENTRY_PROFILES_SAMPLE_RATE,
        send_default_pii=SENTRY_SEND_DEFAULT_PII,
        server_name=config("NODE_HOSTNAME", default=None),
    )

    if SERVICE_NAME := config("SERVICE_NAME", default=None):
        sentry_sdk.set_tag("service_name", SERVICE_NAME)
