import pytest

from automation.agent.prompts import DAIV_SYSTEM_PROMPT
from automation.agent.subagents import CODE_REVIEW_AGENTS_PATH
from tests.integration_tests.prompt_leak import agent_prompt_texts, assert_no_prompt_leak


def _span(text: str, start: int, words: int = 10) -> str:
    return " ".join(text.split()[start : start + words])


def test_a_case_copied_from_the_system_prompt_fails():
    with pytest.raises(ValueError, match="DAIV_SYSTEM_PROMPT"):
        assert_no_prompt_leak(["Please help.", _span(DAIV_SYSTEM_PROMPT.prompt.template, 120)])


def test_a_case_copied_from_a_detector_charter_fails():
    charter = (CODE_REVIEW_AGENTS_PATH / "cr-correctness.md").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="cr-correctness.md"):
        assert_no_prompt_leak([_span(charter, 60)])


def test_an_unrelated_case_passes():
    assert_no_prompt_leak(["What is the latest released version of Django?", "/code-review"])


def test_covers_the_prompts_the_changes_touch():
    names = agent_prompt_texts().keys()

    assert {
        "automation.agent.prompts.DAIV_SYSTEM_PROMPT",
        "langchain.agents.middleware.todo.WRITE_TODOS_SYSTEM_PROMPT",
        "automation.agent.middlewares.skills.SKILLS_SYSTEM_PROMPT",
        "automation.agent.middlewares.web_search.WEB_SEARCH_SYSTEM_PROMPT",
        "explore_system_prompt",
        "output_invariants_system_prompt",
        "automation.agent.middlewares.git_platform.GIT_PLATFORM_SYSTEM_PROMPT",
        "tool_description_overrides.read_file",
        "skills/code-review/SKILL.md",
        "skills/code-review/agents/cr-correctness.md",
    } <= names
