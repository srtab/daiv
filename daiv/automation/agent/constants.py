from enum import StrEnum

from daiv.settings.components import PROJECT_DIR

# Path where the builtin skills are stored in the filesystem to be copied to the repository.
BUILTIN_SKILLS_PATH = PROJECT_DIR / "automation" / "agent" / "skills"

# Path where the skills are stored in repository.
DAIV_SKILLS_PATH = ".daiv/skills"
CURSOR_SKILLS_PATH = ".cursor/skills"
CLAUDE_CODER_SKILLS_PATH = ".claude/skills"
AGENTS_SKILLS_PATH = ".agents/skills"

# Paths where the skills are stored in repository.
SKILLS_SOURCES = [DAIV_SKILLS_PATH, CURSOR_SKILLS_PATH, CLAUDE_CODER_SKILLS_PATH, AGENTS_SKILLS_PATH]

# Path where the memory is stored in repository.
DAIV_MEMORY_PATH = ".daiv/AGENTS.md"


class ModelName(StrEnum):
    """
    `openrouter` provider is the default provider to use any model that is supported by OpenRouter.

    You can also use `anthropic`, `google` or `openai` model providers directly to use any model that is supported
    by Anthropic, Google or OpenAI.
    """

    # Anthropic models
    CLAUDE_OPUS_4_5 = "openrouter:anthropic/claude-opus-4.5"
    CLAUDE_SONNET_4_5 = "openrouter:anthropic/claude-sonnet-4.5"
    CLAUDE_HAIKU_4_5 = "openrouter:anthropic/claude-haiku-4.5"

    # OpenAI models
    GPT_4_1_MINI = "openrouter:openai/gpt-4.1-mini"
    GPT_5_1_CODEX_MINI = "openrouter:openai/gpt-5.1-codex-mini"
    GPT_5_1_CODEX = "openrouter:openai/gpt-5.1-codex"
    GPT_5_1_CODEX_MAX = "openrouter:openai/gpt-5.1-codex-max"
    GPT_5_2 = "openrouter:openai/gpt-5.2"
    GPT_5_2_CODEX = "openrouter:openai/gpt-5.2-codex"

    # z-ai models
    Z_AI_GLM_4_7 = "openrouter:z-ai/glm-4.7"

    # MoonshotAI models
    MOONSHOTAI_KIMI_K2_5 = "openrouter:moonshotai/kimi-k2.5"
