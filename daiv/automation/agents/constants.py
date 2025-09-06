from enum import StrEnum


class ModelName(StrEnum):
    """
    `openrouter` provider is the default provider to use any model that is supported by OpenRouter.

    You can also use `anthropic`, `google` or `openai` model providers directly to use any model that is supported
    by Anthropic, Google or OpenAI.
    """

    CLAUDE_SONNET_4 = "openrouter:anthropic/claude-sonnet-4"
    CLAUDE_OPUS_4 = "openrouter:anthropic/claude-opus-4"
    GPT_4_1 = "openrouter:openai/gpt-4.1"
    GPT_4_1_MINI = "openrouter:openai/gpt-4.1-mini"
    GPT_4_1_NANO = "openrouter:openai/gpt-4.1-nano"
    O4_MINI = "openrouter:openai/o4-mini"
    O3 = "openrouter:openai/o3"
    GPT_5 = "openrouter:openai/gpt-5"
    GPT_5_MINI = "openrouter:openai/gpt-5-mini"
    GPT_5_NANO = "openrouter:openai/gpt-5-nano"
    DEEPSEEK_CHAT_V3_1 = "openrouter:deepseek/deepseek-chat-v3.1"
    GEMINI_2_5_PRO = "openrouter:google/gemini-2.5-pro"
    GROK_CODE_FAST_1 = "openrouter:x-ai/grok-code-fast-1"
