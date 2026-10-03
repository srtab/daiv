from enum import StrEnum

BOT_NAME = "DAIV"
BOT_LABEL = "daiv"
BOT_MAX_LABEL = "daiv-max"
BOT_AUTO_LABEL = "daiv-auto"

SANDBOX_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024

# A cross-project write carries a person's attribution, so the webhook's "is this my own event?"
# check cannot recognise it. Renders as nothing on both platforms.
CROSS_PROJECT_CONTENT_MARKER = "<!-- daiv:cross-project -->"


class CrossProjectOutcome(StrEnum):
    """How one cross-project attempt ended."""

    ALLOWED = "allowed"
    DENIED_NO_ACCESS = "denied_no_access"
    DENIED_NO_CREDENTIAL = "denied_no_credential"
    DENIED_DISABLED = "denied_disabled"
    DENIED_POLICY = "denied_policy"
    ERROR = "error"


# User-facing terminal messages for chat runs. Written by the chat streamer (as the
# RUN_ERROR event message and persisted to Run.error_message), and rendered verbatim in
# the session transcript on reload, so they must never carry raw exception text. The
# sessions transcript annotator reads Run.error_message back and treats the two neutral
# terminations — CANCELLED_BY_USER_MESSAGE and INTERRUPTED_MESSAGE — as the "aborted"
# marker, and anything else on a FAILED run as a genuine "failed" marker.
CANCELLED_BY_USER_MESSAGE = "Stopped by user."
INTERRUPTED_MESSAGE = "Run was interrupted before completing."
RUN_FAILED_MESSAGE = "Run failed. Check server logs for details."

# A worker runs one task to completion before claiming the next, so short user-visible work
# needs its own queue — priority alone cannot get it past an agent run already running.
TASK_QUEUE_DEFAULT = "default"
TASK_QUEUE_INTERACTIVE = "interactive"

# Ordering within the interactive queue. Default-queue tasks are all long, so ranking them
# would only starve whichever lost.
TASK_PRIORITY_TITLING = 20
TASK_PRIORITY_NOTIFICATION = 10


class ModelName(StrEnum):
    """
    `openrouter` provider is the default provider to use any model that is supported by OpenRouter.

    You can also use `anthropic`, `google` or `openai` model providers directly to use any model that is supported
    by Anthropic, Google or OpenAI.

    Only models that have been tested and are working well are listed here for the sake of convenience.
    """

    # Anthropic models
    CLAUDE_OPUS_4_5 = "openrouter:anthropic/claude-opus-4.5"
    CLAUDE_OPUS_4_6 = "openrouter:anthropic/claude-opus-4.6"
    CLAUDE_SONNET_4_5 = "openrouter:anthropic/claude-sonnet-4.5"
    CLAUDE_SONNET_4_6 = "openrouter:anthropic/claude-sonnet-4.6"
    CLAUDE_HAIKU_4_5 = "openrouter:anthropic/claude-haiku-4.5"

    # OpenAI models
    GPT_5_3_CODEX = "openrouter:openai/gpt-5.3-codex"
    GPT_5_4 = "openrouter:openai/gpt-5.4"
    GPT_5_4_MINI = "openrouter:openai/gpt-5.4-mini"
    GPT_5_6_LUNA = "openrouter:openai/gpt-5.6-luna"

    # z-ai models
    Z_AI_GLM_5_1 = "openrouter:z-ai/glm-5.1"
    Z_AI_GLM_5_3_FLASH = "openrouter:z-ai/glm-5.3-flash"

    # minimax models
    MINIMAX_M3 = "openrouter:minimax/minimax-m3"

    # MoonshotAI models
    MOONSHOTAI_KIMI_K2_6 = "openrouter:moonshotai/kimi-k2.6"
    MOONSHOTAI_KIMI_K2_7_CODE = "openrouter:moonshotai/kimi-k2.7-code"

    # DeepSeek models
    DEEPSEEK_V4_FLASH_0731 = "openrouter:deepseek/deepseek-v4-flash-0731"

    # Google models
    GEMINI_3_7_FLASH = "openrouter:google/gemini-3.7-flash"
