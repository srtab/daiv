from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from langchain.chat_models import BaseChatModel
from langchain_core.runnables import Runnable

from automation.agent.base import BaseAgent, ResolvedProvider, parse_model_spec
from automation.agent.chat_models import OPENROUTER_BASE_URL, ChatOpenRouter
from core.models import Provider, ProviderType, ThinkingLevelChoices

if TYPE_CHECKING:
    import httpx


class ConcreteAgent(BaseAgent):
    def compile(self) -> Runnable:
        return Mock(spec=Runnable)


class TestBaseAgent:
    @pytest.fixture
    def mock_init_chat_model(self):
        with patch("automation.agent.base.init_chat_model") as mock:
            mock.return_value = Mock(spec=BaseChatModel)
            yield mock

    def test_default_initialization(self, mock_init_chat_model):
        agent = ConcreteAgent()

        assert agent.checkpointer is None

    def test_custom_initialization(self, mock_init_chat_model):
        checkpointer = Mock(name="RedisSaver")

        agent = ConcreteAgent(checkpointer=checkpointer)

        assert agent.checkpointer == checkpointer


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


@pytest.mark.django_db
class TestGetModelKwargs:
    def _enable_seed(self, slug: str, api_key: str = "k") -> None:
        p = Provider.objects.get(slug=slug)
        p.api_key = api_key
        p.is_enabled = True
        p.save()

    def test_disabled_provider_raises(self):
        Provider.objects.create(
            slug="off", display_name="off", provider_type=ProviderType.OPENAI, api_key="k", is_enabled=False
        )
        with pytest.raises(RuntimeError, match="disabled"):
            BaseAgent.get_model_kwargs(resolved=parse_model_spec("off:gpt-5.4"))

    def test_keyless_provider_raises(self):
        Provider.objects.create(slug="nokey", display_name="No key", provider_type=ProviderType.OPENAI)
        with pytest.raises(RuntimeError, match="no API key"):
            BaseAgent.get_model_kwargs(resolved=parse_model_spec("nokey:gpt-5.4"))

    def test_anthropic_row(self):
        Provider.objects.create(
            slug="anth2",
            display_name="A2",
            provider_type=ProviderType.ANTHROPIC,
            api_key="sk-a",
            base_url="https://proxy.example.com",
        )
        kw = BaseAgent.get_model_kwargs(resolved=parse_model_spec("anth2:claude-haiku-4-5"))
        assert kw["model_provider"] == ProviderType.ANTHROPIC.value
        assert kw["api_key"] == "sk-a"
        assert kw["base_url"] == "https://proxy.example.com"
        assert kw["model"] == "claude-haiku-4-5"

    def test_openai_compatible_with_custom_base_url_defaults_to_chat_completions(self):
        """Custom OpenAI-compatible rows default to ``use_responses_api=False`` because
        most ``/v1/chat/completions``-only servers reject ``/v1/responses``."""
        Provider.objects.create(
            slug="vllm",
            display_name="vLLM",
            provider_type=ProviderType.OPENAI,
            api_key="sk-v",
            base_url="http://localhost:8000/v1",
            extra_headers={"X-Trace": "yes"},
        )
        kw = BaseAgent.get_model_kwargs(resolved=parse_model_spec("vllm:llama-3.3-70b"))
        assert kw["model_provider"] == ProviderType.OPENAI.value
        assert kw["api_key"] == "sk-v"
        assert kw["openai_api_base"] == "http://localhost:8000/v1"
        assert kw["model_kwargs"]["extra_headers"] == {"X-Trace": "yes"}
        assert "use_responses_api" not in kw

    def test_openai_compatible_opted_into_responses_api(self):
        Provider.objects.create(
            slug="bedrock",
            display_name="Bedrock",
            provider_type=ProviderType.OPENAI,
            api_key="sk-b",
            base_url="https://bedrock.example.com/v1",
            use_responses_api=True,
        )
        kw = BaseAgent.get_model_kwargs(resolved=parse_model_spec("bedrock:gpt-5.4"))
        assert kw["use_responses_api"] is True

    def test_seed_openai_row_keeps_responses_api(self):
        """Migration enables Responses API for the locked ``openai`` seed row."""
        self._enable_seed("openai", "sk-o")
        kw = BaseAgent.get_model_kwargs(resolved=parse_model_spec("openai:gpt-5.4"))
        assert kw["use_responses_api"] is True

    def test_verify_ssl_disabled_injects_insecure_http_clients(self):
        import httpx

        Provider.objects.create(
            slug="selfsigned",
            display_name="Self-signed",
            provider_type=ProviderType.OPENAI,
            api_key="sk-s",
            base_url="https://internal.example.test/v1",
            verify_ssl=False,
        )
        kw = BaseAgent.get_model_kwargs(resolved=parse_model_spec("selfsigned:gpt-5.4"))
        assert isinstance(kw["http_client"], httpx.Client)
        assert isinstance(kw["http_async_client"], httpx.AsyncClient)

    def test_verify_ssl_enabled_omits_http_clients(self):
        Provider.objects.create(
            slug="secure",
            display_name="Secure",
            provider_type=ProviderType.OPENAI,
            api_key="sk-x",
            base_url="https://api.example.com/v1",
        )
        kw = BaseAgent.get_model_kwargs(resolved=parse_model_spec("secure:gpt-5.4"))
        assert "http_client" not in kw
        assert "http_async_client" not in kw

    def test_verify_ssl_disabled_on_openrouter_injects_insecure_http_clients(self):
        """OpenRouter routes through ChatOpenAI, which accepts ``http_client``."""
        import httpx

        Provider.objects.create(
            slug="or_insec", display_name="OR", provider_type=ProviderType.OPENROUTER, api_key="sk-or", verify_ssl=False
        )
        kw = BaseAgent.get_model_kwargs(resolved=parse_model_spec("or_insec:z-ai/glm-5"))
        assert isinstance(kw["http_client"], httpx.Client)
        assert isinstance(kw["http_async_client"], httpx.AsyncClient)

    @pytest.mark.parametrize("ptype", [ProviderType.GOOGLE_GENAI, ProviderType.ANTHROPIC])
    def test_verify_ssl_disabled_on_unsupported_type_logs_and_skips(self, caplog, ptype):
        """langchain-anthropic and langchain-google-genai don't expose ``http_client`` —
        toggling verify_ssl=False there is a silent no-op without the warn path."""
        Provider.objects.create(
            slug=f"skip_{ptype.value}", display_name="S", provider_type=ptype, api_key="sk-x", verify_ssl=False
        )
        model_name = "claude-haiku-4-5" if ptype == ProviderType.ANTHROPIC else "gemini-2.5-pro"
        with caplog.at_level("WARNING", logger="daiv.automation"):
            kw = BaseAgent.get_model_kwargs(resolved=parse_model_spec(f"skip_{ptype.value}:{model_name}"))
        assert "http_client" not in kw
        assert any("SDK has no http_client hook" in rec.message for rec in caplog.records)

    def test_insecure_http_clients_closed_when_init_chat_model_fails(self):
        """Construction failures must not leak httpx pools."""

        Provider.objects.create(
            slug="badmodel",
            display_name="Bad",
            provider_type=ProviderType.OPENAI,
            api_key="sk-x",
            base_url="https://x.example/v1",
            verify_ssl=False,
        )
        sync_holder: list[httpx.Client] = []
        orig_init = sync_holder.append  # placeholder; reassigned via patch below

        with patch("automation.agent.base.init_chat_model") as mock_init:
            # Capture the clients before raising so we can assert they were closed.
            def capture_and_raise(**kwargs):
                sync_holder.append(kwargs["http_client"])
                sync_holder.append(kwargs["http_async_client"])
                raise RuntimeError("simulated wrapper construction failure")

            mock_init.side_effect = capture_and_raise

            with pytest.raises(RuntimeError, match="simulated"):
                BaseAgent.get_model(model="badmodel:gpt-5.4")

        assert len(sync_holder) == 2
        sync_client, _async_client = sync_holder
        assert sync_client.is_closed
        del orig_init

    def test_get_model_openrouter_returns_chat_openrouter(self):
        self._enable_seed("openrouter", "sk-or")
        model = BaseAgent.get_model(model="openrouter:anthropic/claude-sonnet-4.6")
        assert isinstance(model, ChatOpenRouter)
        assert model.openai_api_base == OPENROUTER_BASE_URL

    def test_get_model_non_openrouter_is_not_chat_openrouter(self):
        self._enable_seed("anthropic", "sk-a")
        model = BaseAgent.get_model(model="anthropic:claude-sonnet-4-6")
        assert not isinstance(model, ChatOpenRouter)

    def test_openrouter_anthropic_thinking(self):
        self._enable_seed("openrouter", "sk-or")
        kw = BaseAgent.get_model_kwargs(
            resolved=parse_model_spec("openrouter:anthropic/claude-sonnet-4.6"),
            thinking_level=ThinkingLevelChoices.MEDIUM,
        )
        assert kw["openai_api_base"] == "https://openrouter.ai/api/v1"
        assert kw["api_key"] == "sk-or"
        assert "openai_api_key" not in kw
        assert kw["model_kwargs"]["extra_headers"]["HTTP-Referer"]
        assert kw["temperature"] == 1
        assert kw["max_tokens"] == 51_200
        assert kw["extra_body"]["reasoning"]["enabled"] is True
        assert kw["extra_body"]["reasoning"]["effort"] == ThinkingLevelChoices.MEDIUM
        assert "max_tokens" not in kw["extra_body"]["reasoning"]

    def test_openrouter_anthropic_xhigh(self):
        """xhigh on Anthropic-via-OpenRouter passes the effort string straight through;
        ``max_tokens`` stays inside Anthropic's per-model cap (64K)."""
        self._enable_seed("openrouter", "sk-or")
        kw = BaseAgent.get_model_kwargs(
            resolved=parse_model_spec("openrouter:anthropic/claude-sonnet-4.6"),
            thinking_level=ThinkingLevelChoices.XHIGH,
        )
        assert kw["extra_body"]["reasoning"]["effort"] == ThinkingLevelChoices.XHIGH
        assert kw["max_tokens"] == 64_000

    def test_openrouter_generic_thinking(self):
        self._enable_seed("openrouter", "sk-or")
        kw = BaseAgent.get_model_kwargs(
            resolved=parse_model_spec("openrouter:z-ai/glm-5"), thinking_level=ThinkingLevelChoices.HIGH
        )
        assert kw["extra_body"]["reasoning"]["enabled"] is True
        assert kw["extra_body"]["reasoning"]["effort"] == ThinkingLevelChoices.HIGH

    def test_openrouter_generic_xhigh_passthrough(self):
        self._enable_seed("openrouter", "sk-or")
        kw = BaseAgent.get_model_kwargs(
            resolved=parse_model_spec("openrouter:z-ai/glm-5"), thinking_level=ThinkingLevelChoices.XHIGH
        )
        assert kw["extra_body"]["reasoning"]["enabled"] is True
        assert kw["extra_body"]["reasoning"]["effort"] == ThinkingLevelChoices.XHIGH
        # Generic models bypass the per-level max_tokens table; the Anthropic-only
        # path is what sets max_tokens, so confirm it doesn't leak here.
        assert "max_tokens" not in kw

    def test_google_genai(self):
        self._enable_seed("google_genai", "sk-g")
        kw = BaseAgent.get_model_kwargs(resolved=parse_model_spec("google_genai:gemini-2.5-pro"))
        assert kw["model_provider"] == ProviderType.GOOGLE_GENAI.value
        assert kw["api_key"] == "sk-g"
        assert kw["include_thoughts"] is True

    def test_anthropic_no_thinking_sets_default_max_tokens(self):
        self._enable_seed("anthropic", "sk-a")
        kw = BaseAgent.get_model_kwargs(resolved=parse_model_spec("anthropic:claude-sonnet-4-6"))
        assert kw["max_tokens"] == 16_384

    def test_openai_with_thinking_model(self):
        self._enable_seed("openai", "sk-o")
        kw = BaseAgent.get_model_kwargs(
            resolved=parse_model_spec("openai:gpt-5.3-codex"), thinking_level=ThinkingLevelChoices.LOW
        )
        assert kw["temperature"] == 1
        assert kw["reasoning_effort"] == ThinkingLevelChoices.LOW

    def test_openai_xhigh_downmap_to_high(self):
        """OpenAI's native ``reasoning_effort`` rejects ``xhigh``; we downmap to ``high``
        rather than surfacing a 4xx from the upstream call."""
        self._enable_seed("openai", "sk-o")
        kw = BaseAgent.get_model_kwargs(
            resolved=parse_model_spec("openai:gpt-5.3-codex"), thinking_level=ThinkingLevelChoices.XHIGH
        )
        assert kw["reasoning_effort"] == ThinkingLevelChoices.HIGH

    def test_anthropic_xhigh_budget(self):
        """xhigh on direct Anthropic stays at the Sonnet/Haiku 64K cap — the level
        differentiates from HIGH only on OpenRouter, where the ratio formula applies."""
        self._enable_seed("anthropic", "sk-a")
        kw = BaseAgent.get_model_kwargs(
            resolved=parse_model_spec("anthropic:claude-sonnet-4-6"), thinking_level=ThinkingLevelChoices.XHIGH
        )
        assert kw["temperature"] == 1
        assert kw["max_tokens"] == 64_000
        assert kw["thinking"]["type"] == "enabled"
        assert kw["thinking"]["budget_tokens"] == 64_000 - 16_384
