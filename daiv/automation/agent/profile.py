"""DAIV harness profile registration.

Carries DAIV's customizations to upstream ``deepagents.create_deep_agent``:
suppression of upstream's ``BASE_AGENT_PROMPT`` (DAIV ships its own system
prompt via ``dynamic_daiv_system_prompt`` and the upstream content would
otherwise be appended verbatim, causing duplicate identity/Core-Behavior/
Doing-Tasks sections), filesystem tool description overrides, exclusion of
upstream's ``AnthropicPromptCachingMiddleware`` (DAIV ships its own
OpenRouter-aware subclass), and disabling the auto-added ``general-purpose``
subagent (DAIV provides its own pre-compiled one).

``TodoListMiddleware`` is deliberately NOT excluded: deepagents 0.7 stopped
auto-adding it, so DAIV supplies its own instance (with custom guidance) to the
main agent and its subagents and there is nothing to suppress. Keeping a stale
entry is not harmless — ``_verify_excluded_middleware_coverage`` raises
``ValueError`` when an exclusion matches nothing in any assembled stack.

Setting ``base_system_prompt=""`` only suppresses the ``BASE`` slot; built-in
model-level profiles (e.g. ``anthropic:claude-opus-4-7``) only populate
``system_prompt_suffix``, so ``_merge_profiles`` keeps their suffix on top
of the empty base.
"""

from __future__ import annotations

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile

# Class-form exclusion (exact-type match) is *mandatory* for the prompt-cache
# entry: DAIV's subclass shares upstream's ``__name__``, so a string-form
# ``"AnthropicPromptCachingMiddleware"`` entry would match the subclass too and
# drop both, leaving no caching at all.
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware as _UpstreamAnthropicPromptCachingMiddleware

from automation.agent.middlewares.file_system import CUSTOM_TOOL_DESCRIPTIONS

DAIV_HARNESS_PROFILE = HarnessProfile(
    base_system_prompt="",
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
    register_harness_profile("google_genai", DAIV_HARNESS_PROFILE)
