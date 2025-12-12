from automation.agents.base import ThinkingLevel
from automation.agents.plan_and_execute.conf import settings as plan_and_execute_settings
from automation.agents.plan_and_execute.utils import get_plan_and_execute_agent_kwargs
from codebase.repo_config import Models, PlanAndExecuteModelConfig


class TestGetPlanAndExecuteAgentKwargs:
    """Test the get_plan_and_execute_agent_kwargs() function."""

    def test_get_agent_kwargs_without_use_max(self):
        """Test that get_plan_and_execute_agent_kwargs returns default config when use_max=False."""
        models_config = Models()
        kwargs = get_plan_and_execute_agent_kwargs(models_config=models_config, use_max=False)

        assert kwargs["planning_model_names"] == [
            plan_and_execute_settings.PLANNING_MODEL_NAME,
            plan_and_execute_settings.PLANNING_FALLBACK_MODEL_NAME,
        ]
        assert kwargs["execution_model_names"] == [
            plan_and_execute_settings.EXECUTION_MODEL_NAME,
            plan_and_execute_settings.EXECUTION_FALLBACK_MODEL_NAME,
        ]
        assert kwargs["planning_thinking_level"] == plan_and_execute_settings.PLANNING_THINKING_LEVEL
        assert kwargs["execution_thinking_level"] == plan_and_execute_settings.EXECUTION_THINKING_LEVEL

    def test_get_agent_kwargs_with_use_max(self):
        """Test that get_plan_and_execute_agent_kwargs sets high-performance mode when use_max=True."""
        models_config = Models()
        kwargs = get_plan_and_execute_agent_kwargs(models_config=models_config, use_max=True)

        # When use_max=True, the fallback is the regular planning_model from config
        assert kwargs["planning_model_names"] == [
            plan_and_execute_settings.MAX_PLANNING_MODEL_NAME,
            plan_and_execute_settings.PLANNING_MODEL_NAME,
        ]
        # When use_max=True, the fallback is the regular execution_model from config
        assert kwargs["execution_model_names"] == [
            plan_and_execute_settings.MAX_EXECUTION_MODEL_NAME,
            plan_and_execute_settings.EXECUTION_MODEL_NAME,
        ]
        assert kwargs["planning_thinking_level"] == plan_and_execute_settings.MAX_PLANNING_THINKING_LEVEL
        assert kwargs["execution_thinking_level"] == plan_and_execute_settings.MAX_EXECUTION_THINKING_LEVEL

    def test_get_agent_kwargs_does_not_include_skip_approval(self):
        """Test that get_plan_and_execute_agent_kwargs does not set skip_approval."""
        models_config = Models()
        kwargs = get_plan_and_execute_agent_kwargs(models_config=models_config, use_max=False)

        # Note: skip_approval is not in kwargs as it's handled elsewhere
        assert "skip_approval" not in kwargs

    def test_get_agent_kwargs_with_yaml_model_config(self):
        """Test that get_plan_and_execute_agent_kwargs uses YAML model config when available."""
        # Set up YAML model config
        model_config = PlanAndExecuteModelConfig(
            planning_model="openrouter:anthropic/claude-haiku-4.5",
            planning_fallback_model="openrouter:openai/gpt-4.1-mini",
            planning_thinking_level="low",
            execution_model="openrouter:anthropic/claude-haiku-4.5",
            execution_fallback_model="openrouter:openai/gpt-4.1-mini",
            execution_thinking_level=None,
        )
        models_config = Models(plan_and_execute=model_config)
        kwargs = get_plan_and_execute_agent_kwargs(models_config=models_config, use_max=False)

        assert kwargs["planning_model_names"] == [
            "openrouter:anthropic/claude-haiku-4.5",
            "openrouter:openai/gpt-4.1-mini",
        ]
        assert kwargs["execution_model_names"] == [
            "openrouter:anthropic/claude-haiku-4.5",
            "openrouter:openai/gpt-4.1-mini",
        ]
        assert kwargs["planning_thinking_level"] == ThinkingLevel.LOW
        assert kwargs["execution_thinking_level"] is None

    def test_get_agent_kwargs_use_max_overrides_yaml_config(self):
        """Test that use_max=True overrides YAML config."""
        # Set up YAML model config
        model_config = PlanAndExecuteModelConfig(
            planning_model="openrouter:anthropic/claude-haiku-4.5", planning_thinking_level="low"
        )
        models_config = Models(plan_and_execute=model_config)
        kwargs = get_plan_and_execute_agent_kwargs(models_config=models_config, use_max=True)

        # use_max should override YAML config
        assert kwargs["planning_model_names"][0] == plan_and_execute_settings.MAX_PLANNING_MODEL_NAME
        assert kwargs["planning_thinking_level"] == plan_and_execute_settings.MAX_PLANNING_THINKING_LEVEL

    def test_get_agent_kwargs_partial_yaml_config(self):
        """Test that partial YAML config merges with environment defaults."""
        # Set up partial YAML model config (only planning_model)
        model_config = PlanAndExecuteModelConfig(planning_model="openrouter:anthropic/claude-haiku-4.5")
        models_config = Models(plan_and_execute=model_config)
        kwargs = get_plan_and_execute_agent_kwargs(models_config=models_config, use_max=False)

        # planning_model should come from YAML
        assert kwargs["planning_model_names"][0] == "openrouter:anthropic/claude-haiku-4.5"
        # planning_fallback_model should come from env vars
        assert kwargs["planning_model_names"][1] == plan_and_execute_settings.PLANNING_FALLBACK_MODEL_NAME
        # execution_model should come from env vars
        assert kwargs["execution_model_names"][0] == plan_and_execute_settings.EXECUTION_MODEL_NAME
