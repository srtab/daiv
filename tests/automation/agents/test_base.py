from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from langchain.chat_models.base import BaseChatModel
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.runnables import Runnable

from automation.agents.base import BaseAgent, ModelProvider, Usage


class ConcreteAgent(BaseAgent):
    def compile(self) -> Runnable:
        return Mock(spec=Runnable)


class TestBaseAgent:
    @pytest.fixture
    def mock_init_chat_model(self):
        with patch("automation.agents.base.init_chat_model") as mock:
            mock.return_value = Mock(spec=BaseChatModel)
            yield mock

    def test_default_initialization(self, mock_init_chat_model):
        agent = ConcreteAgent()

        assert isinstance(agent.usage_handler, OpenAICallbackHandler)
        assert agent.checkpointer is None

    def test_custom_initialization(self, mock_init_chat_model):
        usage_handler = Mock(spec=OpenAICallbackHandler)
        checkpointer = Mock(name="PostgresSaver")

        agent = ConcreteAgent(usage_handler=usage_handler, checkpointer=checkpointer)

        assert agent.usage_handler == usage_handler
        assert agent.checkpointer == checkpointer

    def test_get_model_kwargs_anthropic(self):
        with patch("automation.agents.base._attempt_infer_model_provider") as mock_provider:
            mock_provider.return_value = ModelProvider.ANTHROPIC
            agent = ConcreteAgent()
            kwargs = agent.get_model_kwargs(model="claude-3-5-sonnet-20240229")

            assert kwargs["temperature"] == 0
            assert kwargs["max_tokens"] == 2048

    def test_get_model_kwargs_openai(self):
        with patch("automation.agents.base._attempt_infer_model_provider") as mock_provider:
            mock_provider.return_value = ModelProvider.OPENAI
            agent = ConcreteAgent()
            kwargs = agent.get_model_kwargs(model="gpt-4")

            assert kwargs["temperature"] == 0
            assert "max_tokens" not in kwargs
            assert not kwargs["model_kwargs"]

    def test_get_max_token_value(self):
        with patch("automation.agents.base._attempt_infer_model_provider") as mock_provider:
            mock_provider.return_value = ModelProvider.ANTHROPIC

            agent = ConcreteAgent()
            assert agent.get_max_token_value(model_name="claude-3-5-sonnet-20240229") == 8192

            agent = ConcreteAgent()
            assert agent.get_max_token_value(model_name="claude-3-opus-20240229") == 8192

    def test_invalid_model_provider(self):
        with patch("automation.agents.base._attempt_infer_model_provider") as mock_provider:
            mock_provider.return_value = "invalid_provider"
            agent = ConcreteAgent()

            with pytest.raises(ValueError, match="Unknown provider for model"):
                agent.get_max_token_value(model_name="invalid_model")


class TestUsage:
    def test_usage_addition(self):
        usage1 = Usage(
            completion_tokens=100,
            prompt_tokens=200,
            total_tokens=300,
            prompt_cost=Decimal("0.1"),
            completion_cost=Decimal("0.2"),
            total_cost=Decimal("0.3"),
        )

        usage2 = Usage(
            completion_tokens=50,
            prompt_tokens=100,
            total_tokens=150,
            prompt_cost=Decimal("0.05"),
            completion_cost=Decimal("0.1"),
            total_cost=Decimal("0.15"),
        )

        result = usage1 + usage2

        assert result.completion_tokens == 150
        assert result.prompt_tokens == 300
        assert result.total_tokens == 450
        assert result.prompt_cost == Decimal("0.15")
        assert result.completion_cost == Decimal("0.3")
        assert result.total_cost == Decimal("0.45")
