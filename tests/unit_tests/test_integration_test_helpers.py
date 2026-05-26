import pytest

from tests.integration_tests.utils import _resolve_provider_slug


@pytest.mark.parametrize(
    "model_spec,expected_slug",
    [
        ("openrouter:anthropic/claude-sonnet-4.6", "openrouter"),
        ("openrouter:openai/gpt-5.4-mini", "openrouter"),
        ("anthropic:claude-sonnet-4-6", "anthropic"),
        ("google:gemini-2.5-pro", "google"),
        ("openai:gpt-5.4", "openai"),
        ("eurotux:qwen36", "eurotux"),
        # Bare-name heuristics
        ("gpt-5.4", "openai"),
        ("gpt-4-turbo", "openai"),
        ("o4-mini", "openai"),
        ("claude-haiku-4-5", "anthropic"),
        ("gemini-2.5-pro", "google_genai"),
        # Fall-through: unknown bare name returns itself unchanged so callers
        # (parse_model_spec) raise the canonical ValueError.
        ("not-a-known-model", "not-a-known-model"),
    ],
)
def test_resolve_provider_slug(model_spec: str, expected_slug: str) -> None:
    assert _resolve_provider_slug(model_spec) == expected_slug
