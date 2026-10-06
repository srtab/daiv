from langchain_core.messages import AIMessage, ToolMessage

from tests.integration_tests.coverage_grading import (
    absolute_paths,
    calls_outside,
    one_step_violation,
    search_backend_failed,
    todo_planning_violation,
    trailing_source_links,
)


def calls(*names: str) -> list[dict]:
    return [{"name": name, "args": {}, "id": str(index)} for index, name in enumerate(names)]


class TestTodoPlanning:
    def test_planning_before_the_first_edit_passes(self):
        assert todo_planning_violation(calls("read_file", "write_todos", "edit_file")) is None

    def test_planning_in_the_same_turn_ahead_of_the_edit_passes(self):
        assert todo_planning_violation(calls("write_todos", "edit_file", "write_todos")) is None

    def test_planning_without_editing_yet_passes(self):
        assert todo_planning_violation(calls("grep", "write_todos")) is None

    def test_an_edit_before_planning_fails(self):
        assert "edit_file ran before write_todos" in todo_planning_violation(calls("edit_file", "write_todos"))

    def test_never_planning_fails(self):
        assert "never called" in todo_planning_violation(calls("read_file", "write_file"))


class TestOneStep:
    def test_editing_without_planning_passes(self):
        assert one_step_violation(calls("read_file", "edit_file")) is None

    def test_a_shell_edit_without_planning_passes(self):
        assert one_step_violation(calls("bash")) is None

    def test_planning_fails(self):
        assert "write_todos was called" in one_step_violation(calls("write_todos", "edit_file"))

    def test_never_trying_the_edit_fails(self):
        assert "never tried the edit" in one_step_violation(calls("read_file"))


class TestSearchBackendFailed:
    def test_every_result_an_error_is_a_backend_failure(self):
        messages = [ToolMessage("error: rate limited", name="web_search", tool_call_id="1")]

        assert search_backend_failed(messages, "web_search")

    def test_one_good_result_is_not(self):
        messages = [
            ToolMessage("error: rate limited", name="web_search", tool_call_id="1"),
            ToolMessage(
                '[{"title": "Django", "link": "https://djangoproject.com"}]', name="web_search", tool_call_id="2"
            ),
        ]

        assert not search_backend_failed(messages, "web_search")

    def test_no_search_at_all_is_not(self):
        assert not search_backend_failed([AIMessage("Django 5.2")], "web_search")


class TestTrailingSourceLinks:
    def test_a_reply_ending_with_a_linked_sources_list(self):
        reply = "Django 6.0.1 is the latest.\n\nSources:\n- [Django releases](https://djangoproject.com/download/)\n"

        assert trailing_source_links(reply) == ["- [Django releases](https://djangoproject.com/download/)"]

    def test_bold_and_heading_forms_of_the_label(self):
        for label in ("**Sources:**", "### Sources", "**Sources**:"):
            assert trailing_source_links(f"Answer.\n\n{label}\n1. [PyPI](https://pypi.org/project/Django/)")

    def test_prose_after_the_list_means_the_reply_does_not_end_with_sources(self):
        reply = "Answer.\n\nSources:\n- [PyPI](https://pypi.org/project/Django/)\n\nLet me know if you need more."

        assert trailing_source_links(reply) == []

    def test_unlinked_items_do_not_count(self):
        assert trailing_source_links("Answer.\n\nSources:\n- the Django website") == []

    def test_no_sources_section(self):
        assert trailing_source_links("Django 6.0.1 is the latest.") == []


def test_calls_outside_names_each_foreign_call():
    assert calls_outside(calls("grep", "bash", "read_file", "edit_file"), {"grep", "read_file"}) == [
        "bash",
        "edit_file",
    ]


def test_absolute_paths_finds_paths_under_the_root_only():
    report = "Enforced in /workspace/repo/daiv/automation/agent/middlewares/step_budget.py and in daiv/other.py."

    assert absolute_paths(report, "/workspace/repo") == [
        "/workspace/repo/daiv/automation/agent/middlewares/step_budget.py"
    ]


def test_absolute_paths_drops_a_sentence_final_period():
    report = "See /workspace/repo/daiv/automation/agent/config.py. Then check /workspace/repo/daiv/core/redis.py."

    assert absolute_paths(report, "/workspace/repo") == [
        "/workspace/repo/daiv/automation/agent/config.py",
        "/workspace/repo/daiv/core/redis.py",
    ]
