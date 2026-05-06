"""DAIV harness profile registration.

Carries DAIV's customizations to upstream ``deepagents.create_deep_agent``:
filesystem tool description overrides, exclusion of upstream's
``AnthropicPromptCachingMiddleware`` (DAIV ships its own OpenRouter-aware
subclass), and disabling the auto-added ``general-purpose`` subagent (DAIV
provides its own pre-compiled one).
"""

from __future__ import annotations

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile

# Class-form exclusion (exact-type match). DAIV's subclass shares the same
# ``__name__`` as upstream, so string-form would match both — class-form is
# mandatory here.
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware as _UpstreamAnthropicPromptCachingMiddleware

from automation.agent.middlewares.file_system import CUSTOM_TOOL_DESCRIPTIONS

DAIV_HARNESS_PROFILE = HarnessProfile(
    tool_description_overrides=CUSTOM_TOOL_DESCRIPTIONS,
    excluded_middleware=frozenset({_UpstreamAnthropicPromptCachingMiddleware}),
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
