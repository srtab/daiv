from unittest.mock import Mock, patch

import pytest
from langchain.chat_models import BaseChatModel
from langchain_core.runnables import Runnable

from automation.agent.base import BaseAgent, ResolvedProvider, parse_model_spec
from core.models import Provider, ProviderType, ThinkingLevelChoices


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

    def test_openai_compatible_with_custom_base_url(self):
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
        assert kw["use_responses_api"] is True

    def test_openrouter_anthropic_thinking(self):
        self._enable_seed("openrouter", "sk-or")
        kw = BaseAgent.get_model_kwargs(
            resolved=parse_model_spec("openrouter:anthropic/claude-sonnet-4.6"),
            thinking_level=ThinkingLevelChoices.MEDIUM,
        )
        assert kw["openai_api_base"] == "https://openrouter.ai/api/v1"
        assert kw["model_kwargs"]["extra_headers"]["HTTP-Referer"]
        assert kw["temperature"] == 1
        assert "max_tokens" in kw
        assert kw["extra_body"]["reasoning"]["max_tokens"] >= 1024

    def test_openrouter_generic_thinking(self):
        self._enable_seed("openrouter", "sk-or")
        kw = BaseAgent.get_model_kwargs(
            resolved=parse_model_spec("openrouter:z-ai/glm-5"), thinking_level=ThinkingLevelChoices.HIGH
        )
        assert kw["extra_body"]["reasoning"]["enabled"] is True
        assert kw["extra_body"]["reasoning"]["effort"] == ThinkingLevelChoices.HIGH

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
