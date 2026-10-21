from codebase.clients.gitlab.utils import replace_section_start_and_end_markers


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
