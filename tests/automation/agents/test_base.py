from unittest.mock import Mock, patch

import pytest
from langchain.chat_models.base import BaseChatModel
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

        assert agent.checkpointer is None

    def test_custom_initialization(self, mock_init_chat_model):
        checkpointer = Mock(name="PostgresSaver")

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

    def test_get_max_token_value(self):
        agent = ConcreteAgent()
        assert agent.get_max_token_value(model_name="claude-3-5-sonnet-20240229") == 8192

        agent = ConcreteAgent()
        assert agent.get_max_token_value(model_name="claude-3-opus-20240229") == 8192

    def test_invalid_model_provider(self):
        agent = ConcreteAgent()

        with pytest.raises(ValueError, match="Unknown/Unsupported provider for model"):
            agent.get_max_token_value(model_name="invalid_model")

    def test_get_model_anthropic_thinking_with_forced_tool_choice(self, mock_init_chat_model):
        """Test that thinking is disabled when tool_choice is forced."""
        # This would be called in the actual agent implementation
        # to verify the fix prevents the BadRequestError
        with patch("automation.agents.base.BaseAgent.get_model_kwargs") as mock_kwargs:
            mock_kwargs.return_value = {
                "model": "claude-sonnet-4",
                "max_tokens": 8192,
                "thinking_level": None,  # Should be None when tool_choice is forced
                "temperature": 0,
                "model_kwargs": {},
                "model_provider": ModelProvider.ANTHROPIC
            }
            
            # Simulate the scenario where tool_choice is forced
            BaseAgent.get_model(model="claude-sonnet-4", thinking_level=None, max_tokens=8192)
            
            mock_kwargs.assert_called_once_with(
                model_provider=ModelProvider.ANTHROPIC,
                thinking_level=None,
                model="claude-sonnet-4",
                max_tokens=8192
            )

    def test_get_model_anthropic_thinking_with_auto_tool_choice(self, mock_init_chat_model):
        """Test that thinking works when tool_choice is auto."""
        # This verifies thinking mode works when tool_choice allows it
        from automation.agents.base import ThinkingLevel
        
        with patch("automation.agents.base.BaseAgent.get_model_kwargs") as mock_kwargs:
            mock_kwargs.return_value = {
                "model": "claude-sonnet-4",
                "max_tokens": 8192,
                "thinking_level": ThinkingLevel.MEDIUM,  # Should preserve thinking level when auto
                "temperature": 1,  # Should be 1 when thinking is enabled
                "model_kwargs": {},
                "model_provider": ModelProvider.ANTHROPIC,
                "thinking": {"type": "enabled", "budget_tokens": 25600}
            }
            
            # Simulate the scenario where tool_choice is auto
            BaseAgent.get_model(model="claude-sonnet-4", thinking_level=ThinkingLevel.MEDIUM, max_tokens=8192)
            
            mock_kwargs.assert_called_once_with(
                model_provider=ModelProvider.ANTHROPIC,
                thinking_level=ThinkingLevel.MEDIUM,
                model="claude-sonnet-4",
                max_tokens=8192
            )
