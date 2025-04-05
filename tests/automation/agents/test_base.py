from unittest.mock import Mock, patch

import pytest
from langchain.chat_models.base import BaseChatModel
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.runnables import Runnable

from automation.agents.base import BaseAgent, ModelProvider


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
