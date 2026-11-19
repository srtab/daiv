from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import anthropic
import openai
from google.genai import errors as genai_errors

from core.models import ProviderFailureKind

if TYPE_CHECKING:
    from core.provider_failures import ProviderFailure

TAG_ATTR = "daiv_provider_failure"

_IN_FLIGHT_BUDGET = "openrouter_in_flight_budget"
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


def _classify_status(status: int, error: dict[str, Any]) -> ProviderFailureKind | None:
    limit_source = _limit_source(error)
    details = error.get("details")
    error_code = details.get("error_code") if isinstance(details, dict) else None
    match status:
        case 401:
            return ProviderFailureKind.AUTH_FAILED
        case 402:
            if limit_source == _IN_FLIGHT_BUDGET:
                return ProviderFailureKind.RATE_LIMITED
            return ProviderFailureKind.OUT_OF_CREDITS
        case 403:
            if limit_source == "openrouter_key_limit" or limit_source.endswith("_budget"):
                return ProviderFailureKind.SPEND_LIMIT_REACHED
            return None
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
    stack = [exc]
    while stack:
        current = stack.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        stack.extend([current.__cause__, current.__context__])


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
