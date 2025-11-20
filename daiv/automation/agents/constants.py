from enum import StrEnum


class ModelName(StrEnum):
    """
    `openrouter` provider is the default provider to use any model that is supported by OpenRouter.

    You can also use `anthropic`, `google` or `openai` model providers directly to use any model that is supported
    by Anthropic, Google or OpenAI.
    """

    # Anthropic models
    CLAUDE_OPUS_4_1 = "openrouter:anthropic/claude-opus-4.1"
    CLAUDE_SONNET_4_5 = "openrouter:anthropic/claude-sonnet-4.5"
    CLAUDE_HAIKU_4_5 = "openrouter:anthropic/claude-haiku-4.5"

    # OpenAI models
    O3 = "openrouter:openai/o3"
    GPT_4_1 = "openrouter:openai/gpt-4.1"
    GPT_4_1_MINI = "openrouter:openai/gpt-4.1-mini"
    GPT_4_1_NANO = "openrouter:openai/gpt-4.1-nano"
    GPT_5 = "openrouter:openai/gpt-5"
    GPT_5_MINI = "openrouter:openai/gpt-5-mini"
    GPT_5_NANO = "openrouter:openai/gpt-5-nano"
    GPT_5_CODEX = "openrouter:openai/gpt-5-codex"

    # DeepSeek models
    DEEPSEEK_CHAT_V3_1_TERMINUS = "openrouter:deepseek/deepseek-v3.1-terminus:exacto"

    # Google models
    GEMINI_2_5_PRO = "openrouter:google/gemini-2.5-pro"

    # x-ai models
    GROK_CODE_FAST_1 = "openrouter:x-ai/grok-code-fast-1"

    # z-ai models
    Z_AI_GLM_4_6 = "openrouter:z-ai/glm-4.6:exacto"

    # Qwen models
    QWEN_3_MAX = "openrouter:qwen/qwen3-max"
    QWEN_3_CODER_PLUS = "openrouter:qwen/qwen3-coder-plus"

    # MoonshotAI models
    MOONSHOTAI_KIMI_K2_THINKING = "openrouter:moonshotai/kimi-k2-thinking"

    # Minimax models
    MINIMAX_M2 = "openrouter:minimax/minimax-m2"
