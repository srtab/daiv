from unittest.mock import Mock, patch

import pytest
from langchain.chat_models import BaseChatModel
from langchain_core.runnables import Runnable

from automation.agent.base import BaseAgent, ModelProvider, ThinkingLevel


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
        assert kwargs["max_tokens"] == 4_096

    def test_get_model_kwargs_openai(self):
        agent = ConcreteAgent()
        kwargs = agent.get_model_kwargs(model_provider=ModelProvider.OPENAI, model="gpt-4")

        assert kwargs["temperature"] == 0
        assert "max_tokens" not in kwargs
        assert not kwargs["model_kwargs"]

    def test_get_model_kwargs_openrouter(self):
        agent = ConcreteAgent()
        kwargs = agent.get_model_kwargs(
            model_provider=ModelProvider.OPENROUTER, model="openrouter:anthropic/claude-sonnet-4.5"
        )

        assert kwargs["model"] == "anthropic/claude-sonnet-4.5"
        assert kwargs["model_provider"] == ModelProvider.OPENROUTER
        assert kwargs["temperature"] == 0
        assert kwargs["app_url"] == "https://srtab.github.io/daiv"
        assert "app_title" in kwargs
        assert "openrouter_api_key" in kwargs
        assert "openai_api_base" not in kwargs
        assert "openai_api_key" not in kwargs
        assert "extra_body" not in kwargs
        assert kwargs["max_tokens"] == 4_096  # anthropic/ prefix triggers default max_tokens

    def test_get_model_kwargs_openrouter_with_thinking(self):
        agent = ConcreteAgent()
        kwargs = agent.get_model_kwargs(
            model_provider=ModelProvider.OPENROUTER,
            model="openrouter:anthropic/claude-sonnet-4.5",
            thinking_level=ThinkingLevel.XHIGH,
        )

        assert kwargs["reasoning"] == {"effort": "xhigh"}
        assert kwargs["temperature"] == 1

    def test_get_model_kwargs_openrouter_openai_with_thinking(self):
        agent = ConcreteAgent()
        kwargs = agent.get_model_kwargs(
            model_provider=ModelProvider.OPENROUTER,
            model="openrouter:openai/gpt-5.2",
            thinking_level=ThinkingLevel.HIGH,
        )

        assert kwargs["reasoning"] == {"effort": "high"}
        assert kwargs["temperature"] == 1

    def test_get_max_token_value(self):
        agent = ConcreteAgent()
        assert agent.get_max_token_value(model_name="claude-3-5-sonnet-20240229") == 8192

        agent = ConcreteAgent()
        assert agent.get_max_token_value(model_name="claude-3-opus-20240229") == 8192

    def test_get_max_token_value_openrouter(self):
        agent = ConcreteAgent()
        assert agent.get_max_token_value(model_name="openrouter:anthropic/claude-sonnet-4.5") == 8192

    def test_invalid_model_provider(self):
        agent = ConcreteAgent()

        with pytest.raises(ValueError, match="Unknown/Unsupported provider for model"):
            agent.get_max_token_value(model_name="invalid_model")

    def test_thinking_level_xhigh_value(self):
        assert ThinkingLevel.XHIGH == "xhigh"

    def test_get_anthropic_thinking_tokens_xhigh(self):
        max_tokens, thinking_tokens = BaseAgent._get_anthropic_thinking_tokens(
            thinking_level=ThinkingLevel.XHIGH, max_tokens=4_096
        )
        assert thinking_tokens == 64_000
        assert max_tokens == 64_000 + 4_096
