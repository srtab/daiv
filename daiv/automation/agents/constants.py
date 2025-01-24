from enum import StrEnum


class ModelName(StrEnum):
    CLAUDE_3_5_SONNET_20241022 = "claude-3-5-sonnet-20241022"
    CLAUDE_3_5_HAIKU_20241022 = "claude-3-5-haiku-20241022"
    GPT_4O_2024_11_20 = "gpt-4o-2024-11-20"
    GPT_4O_MINI_2024_07_18 = "gpt-4o-mini-2024-07-18"
    GEMINI_2_0_FLASH_EXP = "gemini-2.0-flash-exp"
    GEMINI_2_0_FLASH_THINKING_EXP_01_21 = "gemini-2.0-flash-thinking-exp-01-21"
    DEEPSEEK_CHAT = "deepseek-chat"
    DEEPSEEK_REASONER = "deepseek-reasoner"
