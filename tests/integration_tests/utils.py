import os
from typing import TYPE_CHECKING

import pytest
from langchain.messages import AIMessage

from automation.agent.constants import ModelName

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage
    from langchain_core.tools import ToolCall

INTERRUPT_ALL_TOOLS_CONFIG = {
    # SkillMiddleware
    "skill": True,
    # TodoListMiddleware
    "write_todos": True,
    # FilesystemMiddleware
    "grep": True,
    "glob": True,
    "ls": True,
    "read_file": True,
    "edit_file": True,
    "write_file": True,
    # SubAgentMiddleware
    "task": True,
    # SandboxMiddleware
    "bash": True,
    # WebFetchMiddleware
    "web_fetch": True,
    # WebSearchMiddleware
    "web_search": True,
    # GitPlatformMiddleware
    "github": True,
    "gitlab": True,
}

CODING_MODEL_NAMES = [
    ModelName.CLAUDE_SONNET_4_5,
    ModelName.CLAUDE_SONNET_4_6,
    ModelName.CLAUDE_OPUS_4_5,
    ModelName.CLAUDE_OPUS_4_6,
    ModelName.GPT_5_3_CODEX,
    ModelName.GPT_5_4,
    ModelName.Z_AI_GLM_5_1,
    ModelName.MINIMAX_M3,
    ModelName.MOONSHOTAI_KIMI_K2_6,
]

# What production runs for this task (`diff_to_metadata_model_name` and its fallback). The suite
# defaults to these two: at 12 cases each, the full candidate list is 84 paid runs and ~20 minutes.
_PRODUCTION_MODELS = [ModelName.GEMINI_3_7_FLASH, ModelName.DEEPSEEK_V4_FLASH_0731]

_CANDIDATE_MODELS = [
    ModelName.GPT_5_4_MINI,
    ModelName.CLAUDE_HAIKU_4_5,
    ModelName.GPT_5_6_LUNA,
    ModelName.Z_AI_GLM_5_3_FLASH,
    ModelName.MOONSHOTAI_KIMI_K2_7_CODE,
]

# Set DAIV_EVAL_ALL_MODELS=1 to score the candidates too, e.g. before changing the site default.
FAST_MODEL_NAMES = (
    _PRODUCTION_MODELS + _CANDIDATE_MODELS if os.environ.get("DAIV_EVAL_ALL_MODELS") else _PRODUCTION_MODELS
)

_PROVIDER_ENV_VAR = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "google": "GOOGLE_API_KEY",  # alias parse_model_spec accepts
    "openrouter": "OPENROUTER_API_KEY",
}

_BARE_PREFIX_TO_SLUG = ((("gpt-4", "gpt-5", "o4"), "openai"), (("claude",), "anthropic"), (("gemini",), "google_genai"))


def _resolve_provider_slug(model_spec: str) -> str:
    if ":" in model_spec:
        return model_spec.split(":", 1)[0]
    for prefixes, slug in _BARE_PREFIX_TO_SLUG:
        if model_spec.startswith(prefixes):
            return slug
    return model_spec


def require_provider_for_model(model_spec: str) -> None:
    """Skip the current test if the provider for ``model_spec`` has no API key.

    Built-in providers map to the canonical env vars (OPENROUTER_API_KEY, etc.).
    Custom providers use the DAIV_TEST_PROVIDER_<SLUG>_API_KEY convention from
    conftest._provision_providers; both must be set for the row to exist.
    """
    slug = _resolve_provider_slug(model_spec)
    env_var = _PROVIDER_ENV_VAR.get(slug)
    if env_var is None:
        env_var = f"DAIV_TEST_PROVIDER_{slug.upper()}_API_KEY"
    if not os.environ.get(env_var):
        pytest.skip(f"{env_var} not set; cannot run against {model_spec!r}.")


def extract_tool_calls(messages: list[BaseMessage]) -> list[ToolCall]:
    return [tool_call for message in messages if isinstance(message, AIMessage) for tool_call in message.tool_calls]


def _memory_models(env_var: str, production: list[ModelName]) -> list[str]:
    """The production pair, or a comma-separated model-spec override from ``env_var``.

    For scoring replacement candidates the two suites are parametrized on: any spec
    ``parse_model_spec`` accepts, not only a ``ModelName``. DAIV_EVAL_ALL_MODELS
    deliberately still does not widen these two suites.
    """
    override = [spec.strip() for spec in os.environ.get(env_var, "").split(",") if spec.strip()]
    return override or list(production)


MEMORY_EXTRACTION_MODELS = _memory_models(
    "DAIV_EVAL_MEMORY_EXTRACTION_MODELS", [ModelName.GPT_5_4_MINI, ModelName.CLAUDE_HAIKU_4_5]
)
MEMORY_CONSOLIDATION_MODELS = _memory_models(
    "DAIV_EVAL_MEMORY_CONSOLIDATION_MODELS", [ModelName.CLAUDE_SONNET_4_6, ModelName.GPT_5_3_CODEX]
)

# In neither matrix above: GPT_5_3_CODEX is a graded consolidation cell and would grade its own
# output. Same vendor as two graded cells, which is acceptable only because the primary gate —
# the decision check — is deterministic and never calls the judge.
MEMORY_JUDGE_MODEL = ModelName.CLAUDE_OPUS_4_6

# A case's result is the majority of its repetitions. 1 is for local iteration and is not a gate.
EVAL_REPEATS = int(os.environ.get("DAIV_EVAL_REPEATS", "3"))
