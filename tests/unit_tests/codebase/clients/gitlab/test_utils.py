import pytest

from codebase.clients.gitlab.utils import replace_section_start_and_end_markers, title_in_draft_state


def test_replace_section_start_and_end_markers():
    """Test that replace_section_start_and_end_markers properly cleans GitLab logs."""
    raw_log = (
        "\x1b[0msection_start:123: step_script\r\n"
        "Running command\x1b[0m\r\n"
        "Output with\rcarriage return\r\n"
        "\x1b[32mColored text\x1b[0m\n"
        "section_end:123: step_script"
    )

    result = replace_section_start_and_end_markers(raw_log)

    assert ">>> step_script" in result
    assert "<<< step_script" in result


@pytest.mark.parametrize(
    ("title", "draft", "expected"),
    [
        pytest.param("feat: add x", True, "Draft: feat: add x", id="draft-adds-prefix"),
        pytest.param("Draft: feat: add x", True, "Draft: feat: add x", id="draft-keeps-a-draft-title"),
        pytest.param("x" * 255, True, "Draft: " + "x" * 248, id="draft-stays-within-gitlabs-255-characters"),
        pytest.param("Draft: feat: add x", False, "feat: add x", id="ready-drops-draft-colon"),
        pytest.param("[Draft] feat: add x", False, "feat: add x", id="ready-drops-bracketed"),
        pytest.param("(draft) feat: add x", False, "feat: add x", id="ready-drops-parenthesised-any-case"),
        pytest.param("feat: add x", False, "feat: add x", id="ready-keeps-a-ready-title"),
        pytest.param("Draft:", False, "Draft:", id="ready-keeps-a-title-that-is-only-the-prefix"),
    ],
)
def test_title_in_draft_state(title, draft, expected):
    assert title_in_draft_state(title, draft=draft) == expected
