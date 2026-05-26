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
    ModelName.Z_AI_GLM_5,
    ModelName.Z_AI_GLM_5_TURBO,
    ModelName.MINIMAX_M2_5,
    ModelName.MINIMAX_M2_7,
    ModelName.MOONSHOTAI_KIMI_K2_5,
]

FAST_MODEL_NAMES = [ModelName.CLAUDE_HAIKU_4_5, ModelName.GPT_5_4_MINI]

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
