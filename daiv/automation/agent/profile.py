"""DAIV harness profile registration.

Carries DAIV's customizations to upstream ``deepagents.create_deep_agent``:
suppression of upstream's ``BASE_AGENT_PROMPT`` (DAIV ships its own system
prompt via ``dynamic_daiv_system_prompt`` and the upstream content would
otherwise be appended verbatim, causing duplicate identity/Core-Behavior/
Doing-Tasks sections), exclusion of the auto-added ``TodoListMiddleware``
(DAIV supplies its own instance with a custom ``system_prompt`` so main
agent and subagents share the same todo guidance), filesystem tool
description overrides, exclusion of upstream's
``AnthropicPromptCachingMiddleware`` (DAIV ships its own OpenRouter-aware
subclass), and disabling the auto-added ``general-purpose`` subagent
(DAIV provides its own pre-compiled one).

Setting ``base_system_prompt=""`` only suppresses the ``BASE`` slot; built-in
model-level profiles (e.g. ``anthropic:claude-opus-4-7``) only populate
``system_prompt_suffix``, so ``_merge_profiles`` keeps their suffix on top
of the empty base.
"""

from __future__ import annotations

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile

# Class-form exclusion (exact-type match). DAIV's subclass shares the same
# ``__name__`` as upstream, so string-form would match both — class-form is
# mandatory here.
from langchain.agents.middleware import TodoListMiddleware
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware as _UpstreamAnthropicPromptCachingMiddleware

from automation.agent.middlewares.file_system import CUSTOM_TOOL_DESCRIPTIONS

DAIV_HARNESS_PROFILE = HarnessProfile(
    base_system_prompt="",
    tool_description_overrides=CUSTOM_TOOL_DESCRIPTIONS,
    excluded_middleware=frozenset({_UpstreamAnthropicPromptCachingMiddleware, TodoListMiddleware}),
    general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
)


def register() -> None:
    """Register the DAIV harness profile under every provider DAIV uses.

    DAIV reaches Anthropic models directly (``anthropic`` provider) and via
    OpenRouter (``openai`` provider, since OpenRouter speaks the OpenAI API).
    Both need the same overrides.
    """
    register_harness_profile("anthropic", DAIV_HARNESS_PROFILE)
    register_harness_profile("openai", DAIV_HARNESS_PROFILE)
    register_harness_profile("google_genai", DAIV_HARNESS_PROFILE)
