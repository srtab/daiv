from enum import StrEnum


class ModelName(StrEnum):
    CLAUDE_3_7_SONNET_20250219 = "claude-3-7-sonnet-20250219"
    CLAUDE_3_5_SONNET_20241022 = "claude-3-5-sonnet-20241022"
    CLAUDE_3_5_HAIKU_20241022 = "claude-3-5-haiku-20241022"
    GPT_4O_2024_11_20 = "gpt-4o-2024-11-20"
    GPT_4O_MINI_2024_07_18 = "gpt-4o-mini-2024-07-18"
    GEMINI_2_0_FLASH = "gemini-2.0-flash"
    GEMINI_2_0_PRO = "gemini-2.0-pro-exp-02-05"
    DEEPSEEK_CHAT = "deepseek-chat"
    DEEPSEEK_REASONER = "deepseek-reasoner"
