from unittest.mock import Mock, patch

import pytest
from langchain.chat_models import BaseChatModel
from langchain_core.runnables import Runnable

from automation.agent.base import BaseAgent, ModelProvider, parse_model_spec


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

    def test_get_model_kwargs_anthropic(self):
        agent = ConcreteAgent()
        kwargs = agent.get_model_kwargs(model_provider=ModelProvider.ANTHROPIC, model="claude-3-5-sonnet-20240229")

        assert kwargs["temperature"] == 0
        assert kwargs["max_tokens"] == 16_384

    def test_get_model_kwargs_openai(self):
        agent = ConcreteAgent()
        kwargs = agent.get_model_kwargs(model_provider=ModelProvider.OPENAI, model="gpt-4")

        assert kwargs["temperature"] == 0
        assert "max_tokens" not in kwargs
        assert not kwargs["model_kwargs"]


class TestParseModelSpec:
    def test_explicit_anthropic_prefix(self):
        provider, model_name = parse_model_spec("anthropic:claude-sonnet-4-6")
        assert provider == ModelProvider.ANTHROPIC
        assert model_name == "claude-sonnet-4-6"

    def test_explicit_openai_prefix(self):
        provider, model_name = parse_model_spec("openai:gpt-5.3-codex")
        assert provider == ModelProvider.OPENAI
        assert model_name == "gpt-5.3-codex"

    def test_explicit_openrouter_prefix(self):
        provider, model_name = parse_model_spec("openrouter:anthropic/claude-sonnet-4.6")
        assert provider == ModelProvider.OPENROUTER
        assert model_name == "anthropic/claude-sonnet-4.6"

    def test_explicit_google_genai_prefix(self):
        provider, model_name = parse_model_spec("google_genai:gemini-2.5-flash")
        assert provider == ModelProvider.GOOGLE_GENAI
        assert model_name == "gemini-2.5-flash"

    def test_google_alias(self):
        provider, model_name = parse_model_spec("google:gemini-2.5-flash")
        assert provider == ModelProvider.GOOGLE_GENAI
        assert model_name == "gemini-2.5-flash"

    def test_bare_claude_name(self):
        provider, model_name = parse_model_spec("claude-sonnet-4-6")
        assert provider == ModelProvider.ANTHROPIC
        assert model_name == "claude-sonnet-4-6"

    def test_bare_gpt_name(self):
        provider, model_name = parse_model_spec("gpt-5.3-codex")
        assert provider == ModelProvider.OPENAI
        assert model_name == "gpt-5.3-codex"

    def test_bare_gemini_name(self):
        provider, model_name = parse_model_spec("gemini-2.5-flash")
        assert provider == ModelProvider.GOOGLE_GENAI
        assert model_name == "gemini-2.5-flash"

    def test_bare_o4_name(self):
        provider, model_name = parse_model_spec("o4-mini")
        assert provider == ModelProvider.OPENAI
        assert model_name == "o4-mini"

    def test_unknown_bare_model_raises(self):
        with pytest.raises(ValueError, match="Unknown/Unsupported provider"):
            parse_model_spec("unknown-model-123")

    def test_unknown_prefix_with_colon_raises(self):
        with pytest.raises(ValueError, match="Unknown provider prefix 'badprefix'"):
            parse_model_spec("badprefix:some-model")

    def test_empty_model_name_after_prefix_raises(self):
        with pytest.raises(ValueError, match="Empty model name"):
            parse_model_spec("openrouter:")

    def test_whitespace_model_name_after_prefix_raises(self):
        with pytest.raises(ValueError, match="Empty model name"):
            parse_model_spec("anthropic:   ")
