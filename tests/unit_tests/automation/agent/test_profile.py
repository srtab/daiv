"""Regression guards for the DAIV harness profile's middleware exclusions.

Every ``excluded_middleware`` entry must correspond to a middleware that is actually
present in some assembled stack: deepagents' ``_verify_excluded_middleware_coverage``
raises ``ValueError`` when an entry matches nothing. deepagents 0.7 stopped auto-adding
``TodoListMiddleware``, so an entry for it is now stale and would break every run —
these tests pin that it is gone and that a real agent still builds.
"""

from deepagents import create_deep_agent
from deepagents._excluded_middleware import _apply_excluded_middleware
from langchain.agents.middleware import TodoListMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware as UpstreamAnthropicPromptCaching

from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from automation.agent.profile import DAIV_HARNESS_PROFILE, register


def test_profile_does_not_exclude_todo_middleware():
    # deepagents 0.7 no longer auto-adds TodoListMiddleware, so DAIV's own instance is the only
    # one in the stack. Excluding the class would both drop that instance (the filter also runs
    # over user-supplied middleware) and trip the matched-nothing coverage check.
    assert TodoListMiddleware not in DAIV_HARNESS_PROFILE.excluded_middleware


def test_daiv_todo_middleware_survives_the_profile_filter():
    daiv_own = TodoListMiddleware(system_prompt="todo guidance")

    filtered = _apply_excluded_middleware([daiv_own], DAIV_HARNESS_PROFILE)

    assert len(filtered) == 1
    assert any(tool.name == "write_todos" for m in filtered for tool in m.tools)


def test_profile_excludes_upstream_prompt_caching_but_keeps_daivs_subclass():
    # Class-form (exact-type) exclusion is mandatory here: DAIV's subclass shares upstream's
    # ``__name__``, so a string-form entry would match both and leave no caching at all.
    assert UpstreamAnthropicPromptCaching in DAIV_HARNESS_PROFILE.excluded_middleware
    assert AnthropicPromptCachingMiddleware not in DAIV_HARNESS_PROFILE.excluded_middleware

    filtered = _apply_excluded_middleware(
        [UpstreamAnthropicPromptCaching(), AnthropicPromptCachingMiddleware()], DAIV_HARNESS_PROFILE
    )

    assert [type(m) for m in filtered] == [AnthropicPromptCachingMiddleware]


def test_real_create_deep_agent_builds_under_the_daiv_profile():
    """End-to-end guard on the profile itself, with nothing patched.

    The rest of the suite patches ``create_deep_agent``, so a profile that upstream rejects
    (a stale ``excluded_middleware`` entry, an unknown field) passes every other test and only
    fails in production. A provider the profile is registered for is required — otherwise the
    DAIV profile never resolves and the assembly this guards is skipped.
    """
    register()
    model = ChatAnthropic(model="claude-sonnet-4-5-20250929", api_key="sk-ant-not-used")

    agent = create_deep_agent(model=model, middleware=[TodoListMiddleware(system_prompt="todo guidance")])

    assert agent is not None
