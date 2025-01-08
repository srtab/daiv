from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from langchain.chat_models.base import BaseChatModel
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.runnables import Runnable

from automation.agents.base import BaseAgent, ModelProvider, Usage
from automation.conf import settings


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

        assert agent.run_name == "ConcreteAgent"
        assert agent.model_name == settings.generic_cost_efficient_model_name
        assert isinstance(agent.usage_handler, OpenAICallbackHandler)
        assert agent.checkpointer is None
        assert agent.model == mock_init_chat_model.return_value

    def test_custom_initialization(self, mock_init_chat_model):
        custom_name = "CustomAgent"
        custom_model = "gpt-4"
        usage_handler = Mock(spec=OpenAICallbackHandler)
        checkpointer = Mock(name="PostgresSaver")

        agent = ConcreteAgent(
            run_name=custom_name, model_name=custom_model, usage_handler=usage_handler, checkpointer=checkpointer
        )

        assert agent.run_name == custom_name
        assert agent.model_name == custom_model
        assert agent.usage_handler == usage_handler
        assert agent.checkpointer == checkpointer

    def test_get_model_kwargs_anthropic(self):
        with patch("automation.agents.base._attempt_infer_model_provider") as mock_provider:
            mock_provider.return_value = ModelProvider.ANTHROPIC
            agent = ConcreteAgent(model_name="claude-3-5-sonnet-20240229")
            kwargs = agent.get_model_kwargs()

            assert kwargs["model"] == "claude-3-5-sonnet-20240229"
            assert kwargs["temperature"] == 0
            assert "anthropic-beta" in kwargs["model_kwargs"]["extra_headers"]
            assert kwargs["max_tokens"] == "2048"

    def test_get_model_kwargs_openai(self):
        with patch("automation.agents.base._attempt_infer_model_provider") as mock_provider:
            mock_provider.return_value = ModelProvider.OPENAI
            agent = ConcreteAgent(model_name="gpt-4")
            kwargs = agent.get_model_kwargs()

            assert kwargs["model"] == "gpt-4"
            assert kwargs["temperature"] == 0
            assert "max_tokens" not in kwargs
            assert not kwargs["model_kwargs"]

    def test_get_max_token_value(self):
        with patch("automation.agents.base._attempt_infer_model_provider") as mock_provider:
            mock_provider.return_value = ModelProvider.ANTHROPIC

            agent = ConcreteAgent(model_name="claude-3-5-sonnet-20240229")
            assert agent.get_max_token_value() == 2048

            agent = ConcreteAgent(model_name="claude-3-opus-20240229")
            assert agent.get_max_token_value() == 2048

    def test_get_config(self):
        agent = ConcreteAgent(run_name="TestAgent")
        config = agent.get_config()

        assert config["run_name"] == "TestAgent"
        assert config["tags"] == ["TestAgent"]
        assert config["metadata"] == {}
        assert config["configurable"] == {}

    def test_invalid_model_provider(self):
        with patch("automation.agents.base._attempt_infer_model_provider") as mock_provider:
            mock_provider.return_value = "invalid_provider"
            agent = ConcreteAgent()

            with pytest.raises(ValueError, match="Unknown provider for model"):
                agent.get_max_token_value()


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
