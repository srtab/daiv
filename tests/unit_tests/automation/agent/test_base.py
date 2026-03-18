from unittest.mock import Mock, patch

import pytest
from langchain.chat_models import BaseChatModel
from langchain_core.runnables import Runnable

from automation.agent.base import BaseAgent, ModelProvider


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
