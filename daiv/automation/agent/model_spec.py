"""Resolve a ``slug:model_name`` spec against the ``Provider`` rows.

Forms, validators, the model picker and run admission import this at URL-load time, so it must stay free
of the LangChain and provider SDK stack: building the chat model lives in :mod:`automation.agent.base`.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.models import Provider, ProviderType

# langchain-anthropic and langchain-google-genai don't expose ``http_client`` /
# ``http_async_client`` as model fields, so ``verify_ssl=False`` can't reach the
# underlying SDK there. Both fall back to the warn-and-skip branch.
HTTPX_CLIENT_PROVIDER_TYPES = frozenset({ProviderType.OPENAI, ProviderType.OPENROUTER})

_BARE_NAME_HEURISTICS = (
    (("gpt-4", "gpt-5", "gpt-6", "o4"), ProviderType.OPENAI.value),
    (("claude",), ProviderType.ANTHROPIC.value),
    (("gemini",), ProviderType.GOOGLE_GENAI.value),
)


class ModelSpecError(ValueError):
    """A model spec that no Provider row serves: an unknown prefix, an empty model name or an unsupported bare name."""


@dataclass(frozen=True)
class ResolvedProvider:
    """Result of resolving a ``slug:model_name`` string against the Provider table."""

    row: Provider.Cached
    model_name: str


def parse_model_spec(model_spec: str) -> ResolvedProvider:
    """
    Parse ``slug:model_name`` against the current Provider rows.

    Order:
      1. Explicit ``slug:model`` where ``slug`` matches a Provider row.
      2. Explicit ``google:model`` alias → ``google_genai`` row.
      3. Bare ``model`` matched by prefix heuristic.

    Raises ``ModelSpecError`` on unknown prefix, empty model name, or missing row.
    """
    rows_by_slug = Provider.get_cached_by_slug()

    if ":" in model_spec:
        prefix, model_name = model_spec.split(":", 1)
        if not model_name.strip():
            raise ModelSpecError(f"Empty model name in spec '{model_spec}'")
        if prefix == "google" and "google_genai" in rows_by_slug:
            return ResolvedProvider(row=rows_by_slug["google_genai"], model_name=model_name)
        if prefix in rows_by_slug:
            return ResolvedProvider(row=rows_by_slug[prefix], model_name=model_name)
        raise ModelSpecError(
            f"Unknown provider prefix '{prefix}' in model spec '{model_spec}'. "
            f"Valid prefixes: {', '.join(sorted(rows_by_slug)) or '(no providers configured)'}"
        )

    for prefixes, target_slug in _BARE_NAME_HEURISTICS:
        if model_spec.startswith(prefixes) and target_slug in rows_by_slug:
            return ResolvedProvider(row=rows_by_slug[target_slug], model_name=model_spec)

    raise ModelSpecError(f"Unknown/Unsupported provider for model {model_spec}")
