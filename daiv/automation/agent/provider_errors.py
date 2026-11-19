from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from core.models import ProviderFailureKind

if TYPE_CHECKING:
    from core.provider_failures import ProviderFailure

TAG_ATTR = "daiv_provider_failure"

_IN_FLIGHT_BUDGET = "openrouter_in_flight_budget"
_CREDITS = "openrouter_credits"
_KEY_LIMIT = "openrouter_key_limit"
_SPEND_LIMIT_ERROR_CODE = "enforced_spend_limit_reached"


def _error_object(body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    error = body.get("error")
    if isinstance(error, dict):
        return error
    return body


def _limit_source(error: dict[str, Any]) -> str:
    metadata = error.get("metadata")
    source = metadata.get("limit_source") if isinstance(metadata, dict) else None
    return source if isinstance(source, str) else ""


def _classify_limit_source(limit_source: str) -> ProviderFailureKind | None:
    if limit_source == _IN_FLIGHT_BUDGET:
        return ProviderFailureKind.RATE_LIMITED
    if limit_source == _CREDITS:
        return ProviderFailureKind.OUT_OF_CREDITS
    if limit_source == _KEY_LIMIT or limit_source.endswith("_budget"):
        return ProviderFailureKind.SPEND_LIMIT_REACHED
    return None


def _classify_status(status: int, error: dict[str, Any]) -> ProviderFailureKind | None:
    limit_source = _limit_source(error)
    details = error.get("details")
    error_code = details.get("error_code") if isinstance(details, dict) else None
    match status:
        case 401:
            return ProviderFailureKind.AUTH_FAILED
        case 402 | 403:
            if limit_source:
                return _classify_limit_source(limit_source)
            return ProviderFailureKind.OUT_OF_CREDITS if status == 402 else None
        case 429:
            if error.get("code") == "insufficient_quota":
                return ProviderFailureKind.OUT_OF_CREDITS
            if error_code == _SPEND_LIMIT_ERROR_CODE:
                return ProviderFailureKind.SPEND_LIMIT_REACHED
            return ProviderFailureKind.RATE_LIMITED
        case _ if status >= 500:
            return ProviderFailureKind.PROVIDER_UNAVAILABLE
    return None


def _classify_one(exc: BaseException) -> ProviderFailureKind | None:
    import anthropic
    import openai
    from google.genai import errors as genai_errors

    if isinstance(exc, openai.APIStatusError | anthropic.APIStatusError):
        error = _error_object(exc.body)
        if (
            isinstance(exc, anthropic.APIStatusError)
            and exc.status_code == 400
            and "credit balance" in str(error.get("message", "")).lower()
        ):
            return ProviderFailureKind.OUT_OF_CREDITS
        return _classify_status(exc.status_code, error)
    if isinstance(exc, openai.APIConnectionError | anthropic.APIConnectionError):
        return ProviderFailureKind.PROVIDER_UNAVAILABLE
    if isinstance(exc, genai_errors.APIError):
        if exc.code == 400 and "API key not valid" in str(exc.message or ""):
            return ProviderFailureKind.AUTH_FAILED
        return _classify_status(exc.code, {})
    return None


def _chain(exc: BaseException | None):
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None


def classify_provider_error(exc: BaseException) -> ProviderFailureKind | None:
    for candidate in _chain(exc):
        if (kind := _classify_one(candidate)) is not None:
            return kind
    return None


def tag_provider_failure(exc: BaseException, failure: ProviderFailure) -> None:
    with contextlib.suppress(AttributeError, TypeError):
        setattr(exc, TAG_ATTR, failure)


def provider_failure_of(exc: BaseException | None) -> ProviderFailure | None:
    for candidate in _chain(exc):
        failure = getattr(candidate, TAG_ATTR, None)
        if failure is not None:
            return failure
    return None
