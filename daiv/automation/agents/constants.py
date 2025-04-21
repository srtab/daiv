from enum import StrEnum


class ModelName(StrEnum):
    """
    `openrouter` provider is the default provider to use any model that is supported by OpenRouter.

    You can also use `anthropic`, `google` or `openai` model providers directly to use any model that is supported
    by Anthropic, Google or OpenAI.
    """

    CLAUDE_3_7_SONNET = "openrouter:anthropic/claude-3-7-sonnet"
    CLAUDE_3_5_HAIKU = "openrouter:anthropic/claude-3-5-haiku"
    GPT_4_1 = "openrouter:openai/gpt-4.1"
    GPT_4_1_MINI = "openrouter:openai/gpt-4.1-mini"
    GPT_4_1_NANO = "openrouter:openai/gpt-4.1-nano"
    O4_MINI = "openrouter:openai/o4-mini"
    DEEPSEEK_CHAT_V3_0324 = "openrouter:deepseek/deepseek-chat-v3-0324"
