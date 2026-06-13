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

from automation.agent.middlewares.file_system import CUSTOM_TOOL_DESCRIPTIONS, DAIVFilesystemMiddleware

DAIV_HARNESS_PROFILE = HarnessProfile(
    base_system_prompt="",
    tool_description_overrides=CUSTOM_TOOL_DESCRIPTIONS,
    excluded_middleware=frozenset({_UpstreamAnthropicPromptCachingMiddleware, TodoListMiddleware}),
    general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
)


def _install_daiv_filesystem_middleware() -> None:
    """Make ``create_deep_agent`` build DAIV's ``FilesystemMiddleware`` subclass for the main agent.

    DAIV needs the main agent's ``grep`` tool to expose an extended (ripgrep) signature
    (``head_limit``/``case_insensitive``/``multiline``), which requires overriding
    ``FilesystemMiddleware._create_grep_tool`` — see ``DAIVFilesystemMiddleware``.

    The usual customization path (the ``TodoListMiddleware`` / ``AnthropicPromptCachingMiddleware``
    precedent: exclude the auto-added upstream middleware via ``excluded_middleware`` and re-add
    DAIV's own instance) does NOT work for ``FilesystemMiddleware``: upstream lists it in
    ``deepagents.graph._REQUIRED_MIDDLEWARE`` (required scaffolding), so ``_apply_excluded_middleware``
    raises ``ValueError`` if it appears in ``excluded_middleware``. Appending a second
    ``FilesystemMiddleware``-family instance instead is also wrong — it would inject the filesystem
    system prompt twice and run the large-result/HumanMessage eviction hooks twice per turn
    (``wrap_model_call`` / ``wrap_tool_call`` compose, they don't replace).

    ``create_deep_agent`` instantiates the auto-added main-agent middleware via the
    ``FilesystemMiddleware`` name bound in ``deepagents.graph``. Rebinding that name to the subclass
    is therefore the lowest-risk mechanism that yields a *single*, correctly-wired instance
    constructed with the exact kwargs upstream passes (``backend`` / ``custom_tool_descriptions`` /
    ``_permissions``). It is a module-global substitution applied once at app startup (here, beside
    the harness-profile registration, itself a global-registry mutation). The subagents build their
    middleware stacks directly (``subagents.py``) and reference ``DAIVFilesystemMiddleware`` by name,
    so they do not rely on this rebinding. Pinned to ``deepagents==0.5.9``; a version bump that
    renames or relocates this auto-add must be re-checked (the tool-name parity guard test will flag
    a changed filesystem tool surface, but not a relocated instantiation).
    """
    import deepagents.graph as _deepagents_graph

    # Intentional name rebind (a subclass replacing the base) — see this function's docstring.
    _deepagents_graph.FilesystemMiddleware = DAIVFilesystemMiddleware  # ty: ignore[invalid-assignment]


def register() -> None:
    """Register the DAIV harness profile under every provider DAIV uses.

    DAIV reaches Anthropic models directly (``anthropic`` provider) and via
    OpenRouter (``openai`` provider, since OpenRouter speaks the OpenAI API).
    Both need the same overrides.
    """
    _install_daiv_filesystem_middleware()
    register_harness_profile("anthropic", DAIV_HARNESS_PROFILE)
    register_harness_profile("openai", DAIV_HARNESS_PROFILE)
    register_harness_profile("google_genai", DAIV_HARNESS_PROFILE)
