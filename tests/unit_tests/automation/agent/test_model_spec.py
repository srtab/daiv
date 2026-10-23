import pytest

from automation.agent.model_spec import ResolvedProvider, parse_model_spec
from core.models import Provider, ProviderType


@pytest.mark.django_db
class TestParseModelSpec:
    def test_parse_resolves_seed_slug(self):
        resolved = parse_model_spec("anthropic:claude-sonnet-4-6")
        assert isinstance(resolved, ResolvedProvider)
        assert resolved.row.slug == "anthropic"
        assert resolved.row.provider_type == ProviderType.ANTHROPIC
        assert resolved.model_name == "claude-sonnet-4-6"

    def test_parse_resolves_user_added_slug(self):
        Provider.objects.create(slug="vllm", display_name="vLLM", provider_type=ProviderType.OPENAI, api_key="k")
        resolved = parse_model_spec("vllm:llama-3.3-70b")
        assert resolved.row.slug == "vllm"
        assert resolved.model_name == "llama-3.3-70b"

    def test_parse_google_alias(self):
        resolved = parse_model_spec("google:gemini-2.5-pro")
        assert resolved.row.slug == "google_genai"
        assert resolved.model_name == "gemini-2.5-pro"

    def test_parse_bare_name_heuristic(self):
        assert parse_model_spec("gpt-5.4").row.slug == "openai"
        assert parse_model_spec("gpt-6-luna").row.slug == "openai"
        assert parse_model_spec("claude-haiku-4-5").row.slug == "anthropic"
        assert parse_model_spec("gemini-2.5-pro").row.slug == "google_genai"

    def test_parse_unknown_prefix_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            parse_model_spec("notaprovider:foo")

    def test_parse_empty_model_name_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            parse_model_spec("anthropic:")

    def test_parse_whitespace_model_name_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            parse_model_spec("anthropic:   ")

    def test_parse_bare_o4(self):
        assert parse_model_spec("o4-mini").row.slug == "openai"

    @pytest.mark.parametrize(
        "model_spec",
        [
            "anthropic:claude-sonnet-4-6",
            "openai:gpt-5.4",
            "google_genai:gemini-2.5-pro",
            "openrouter:anthropic/claude-sonnet-4.6",
            "claude-haiku-4-5",
            "gpt-5.4",
            "gpt-6-luna",
            "gemini-2.5-pro",
            "o4-mini",
        ],
    )
    def test_resolve_provider_slug_matches_parse_model_spec(self, model_spec: str) -> None:
        """The integration-test helper must resolve to the same row parse_model_spec picks.

        If a future change adds a new built-in slug or bare-name heuristic to
        parse_model_spec, the helper in tests/integration_tests/utils.py must
        be updated in lockstep. This test makes drift visible.
        """
        from tests.integration_tests.utils import _resolve_provider_slug

        expected_row = parse_model_spec(model_spec).row
        helper_slug = _resolve_provider_slug(model_spec)
        assert expected_row.slug == helper_slug
