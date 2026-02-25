from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agent.base import ThinkingLevel  # noqa: TC001
from automation.agent.constants import ModelName


class DAIVAgentSettings(BaseSettings):
    """
    Settings for the deep agent.
    """

    model_config = SettingsConfigDict(env_prefix="DAIV_AGENT_", env_parse_none_str="None")

    RECURSION_LIMIT: int = Field(default=500, description="Recursion limit for the agent.")
    MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_SONNET_4_5,
        description="Model for tasks, a multi-modal (image and text) model with capabilities to call tools.",
    )
    FALLBACK_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_5_2, description="Fallback model for tasks if the primary model fails."
    )
    THINKING_LEVEL: ThinkingLevel | None = Field(
        default=ThinkingLevel.MEDIUM,
        description="Thinking level to be used for tasks. Set as `None` to disable thinking.",
    )
    MAX_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_OPUS_4_6,
        description=(
            "Model for tasks when daiv-max label is present, a multi-modal (image and text) model with "
            "capabilities to call tools."
        ),
    )
    MAX_THINKING_LEVEL: ThinkingLevel | None = Field(
        default=ThinkingLevel.HIGH,
        description=(
            "Thinking level to be used for tasks when daiv-max label is present. Set as `None` to disable thinking."
        ),
    )
    EXPLORE_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_HAIKU_4_5,
        description="Model for the explore subagent, a fast model with capabilities to call tools.",
    )
    DOCS_RESEARCH_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_5_1_CODEX_MINI,
        description="Model for the docs research subagent, a fast model with capabilities to call tools.",
    )


settings = DAIVAgentSettings()
