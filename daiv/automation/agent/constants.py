from enum import StrEnum

from daiv.settings.components import PROJECT_DIR

# Path where the builtin skills are stored in the filesystem to be copied to the repository.
BUILTIN_SKILLS_PATH = PROJECT_DIR / "automation" / "agent" / "skills"

# Virtual path (under the FilesystemBackend root, sibling to the repo working tree) where
# global skills — builtins and custom globals — are materialized at agent start.
GLOBAL_SKILLS_PATH = "/skills"

# Path where the skills are stored in repository.
CURSOR_SKILLS_PATH = ".cursor/skills"
CLAUDE_CODE_SKILLS_PATH = ".claude/skills"
AGENTS_SKILLS_PATH = ".agents/skills"

# Paths where the skills are stored in repository.
SKILLS_SOURCES = [CURSOR_SKILLS_PATH, CLAUDE_CODE_SKILLS_PATH, AGENTS_SKILLS_PATH]

# Path where the custom subagents are stored in repository.
AGENTS_SUBAGENTS_PATH = ".agents/subagents"

# Paths where the custom subagents are stored in repository.
SUBAGENTS_SOURCES = [AGENTS_SUBAGENTS_PATH]

# Path where the memory is stored in repository.
AGENTS_MEMORY_PATH = ".agents/AGENTS.md"


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

    # z-ai models
    Z_AI_GLM_5 = "openrouter:z-ai/glm-5"
    Z_AI_GLM_5_TURBO = "openrouter:z-ai/glm-5-turbo"

    # minimax models
    MINIMAX_M2_5 = "openrouter:minimax/minimax-m2.5"
    MINIMAX_M2_7 = "openrouter:minimax/minimax-m2.7"

    # MoonshotAI models
    MOONSHOTAI_KIMI_K2_5 = "openrouter:moonshotai/kimi-k2.5"


# Per-provider model name suggestions for the configuration UI datalists.
MODEL_SUGGESTIONS: dict[str, list[str]] = {
    "openrouter": [
        "anthropic/claude-opus-4.6",
        "anthropic/claude-opus-4.5",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-sonnet-4.5",
        "anthropic/claude-haiku-4.5",
        "openai/gpt-5.3-codex",
        "openai/gpt-5.4",
        "openai/gpt-5.4-mini",
        "z-ai/glm-5",
        "z-ai/glm-5-turbo",
        "minimax/minimax-m2.5",
        "minimax/minimax-m2.7",
        "moonshotai/kimi-k2.5",
    ],
    "anthropic": ["claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-haiku-4-5"],
    "openai": ["gpt-5.3-codex", "gpt-5.4", "gpt-5.4-mini"],
    "google_genai": ["gemini-2.5-flash", "gemini-2.5-pro"],
}
