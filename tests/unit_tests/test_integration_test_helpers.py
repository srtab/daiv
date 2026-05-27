import pytest

from tests.integration_tests.utils import _resolve_provider_slug, require_provider_for_model


@pytest.mark.parametrize(
    "model_spec,expected_slug",
    [
        ("openrouter:anthropic/claude-sonnet-4.6", "openrouter"),
        ("openrouter:openai/gpt-5.4-mini", "openrouter"),
        ("anthropic:claude-sonnet-4-6", "anthropic"),
        ("google:gemini-2.5-pro", "google"),
        ("openai:gpt-5.4", "openai"),
        ("customprovider:model-x", "customprovider"),
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


def test_require_provider_skips_when_built_in_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(pytest.skip.Exception, match="OPENROUTER_API_KEY not set"):
        require_provider_for_model("openrouter:anthropic/claude-sonnet-4.6")


def test_require_provider_runs_when_built_in_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-key")
    require_provider_for_model("openrouter:anthropic/claude-sonnet-4.6")  # no raise


def test_require_provider_skips_when_custom_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAIV_TEST_PROVIDER_CUSTOMPROVIDER_API_KEY", raising=False)
    with pytest.raises(pytest.skip.Exception, match="DAIV_TEST_PROVIDER_CUSTOMPROVIDER_API_KEY not set"):
        require_provider_for_model("customprovider:model-x")


def test_require_provider_runs_when_custom_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIV_TEST_PROVIDER_CUSTOMPROVIDER_API_KEY", "real-key")
    require_provider_for_model("customprovider:model-x")  # no raise


def test_require_provider_uses_bare_name_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(pytest.skip.Exception, match="ANTHROPIC_API_KEY not set"):
        require_provider_for_model("claude-haiku-4-5")


def test_discover_custom_slugs_extracts_non_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.integration_tests import utils as integration_utils
    from tests.integration_tests.conftest import _discover_custom_slugs

    monkeypatch.setattr(integration_utils, "CODING_MODEL_NAMES", ["openrouter:anthropic/claude-sonnet-4.6"])
    monkeypatch.setattr(integration_utils, "FAST_MODEL_NAMES", ["customprovider:model-x", "anthropic:claude-haiku-4-5"])

    slugs = _discover_custom_slugs()
    assert slugs == {"customprovider"}
