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


class TestShortCases:
    @pytest.fixture(autouse=True)
    def _prompts(self, monkeypatch):
        monkeypatch.setattr(
            "tests.integration_tests.prompt_leak.agent_prompt_texts",
            lambda: {
                "automation.agent.prompts.EXAMPLE_PROMPT": "Always rename the user model before you touch /init.",
                "skills/example/SKILL.md": "Trigger on: rename the user model, or on /init.",
            },
        )

    @pytest.mark.parametrize("case", ["Rename the user model", "rename the user model before you touch"])
    def test_a_short_case_pasted_whole_into_a_non_skill_prompt_fails(self, case):
        with pytest.raises(ValueError, match=r"EXAMPLE_PROMPT"):
            assert_no_prompt_leak([case])

    def test_the_error_names_the_shared_span(self):
        with pytest.raises(ValueError, match="rename the user model"):
            assert_no_prompt_leak(["Rename the user model"])

    def test_a_short_case_that_only_a_skill_text_contains_passes(self, monkeypatch):
        monkeypatch.setattr(
            "tests.integration_tests.prompt_leak.agent_prompt_texts",
            lambda: {"skills/example/SKILL.md": "Trigger on: rename the user model, or on /init."},
        )

        assert_no_prompt_leak(["Rename the user model"])

    @pytest.mark.parametrize("case", ["/init", "touch /init"])
    def test_a_one_or_two_word_case_never_trips_containment(self, case):
        assert_no_prompt_leak([case])

    def test_a_case_only_partly_in_the_prompt_passes(self):
        assert_no_prompt_leak(["Rename the user model today"])


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
