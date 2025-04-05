from enum import StrEnum


class ModelName(StrEnum):
    """
    `openrouter` provider is the default provider to use any model that is supported by OpenRouter.

    You can also use `anthropic`, `google` or `openai` model providers directly to use any model that is supported
    by Anthropic, Google or OpenAI.
    """

    CLAUDE_3_7_SONNET = "openrouter:anthropic/claude-3-7-sonnet"
    CLAUDE_3_5_HAIKU = "openrouter:anthropic/claude-3-5-haiku"
    GPT_4O = "openrouter:openai/gpt-4o"
    GPT_4O_MINI = "openrouter:openai/gpt-4o-mini"
    O3_MINI = "openrouter:openai/o3-mini"
    GEMINI_2_0_FLASH = "openrouter:google/gemini-2.0-flash-001"
    GEMINI_2_0_FLASH_LITE = "openrouter:google/gemini-2.0-flash-lite-001"
    GEMINI_2_5_PRO_PREVIEW = "openrouter:google/gemini-2.5-pro-preview-03-25"
    DEEPSEEK_CHAT_V3_0324 = "openrouter:deepseek/deepseek-chat-v3-0324"
