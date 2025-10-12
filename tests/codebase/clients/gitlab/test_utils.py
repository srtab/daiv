from codebase.clients.gitlab.utils import extract_last_command_from_gitlab_logs, replace_section_start_and_end_markers


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


def test_extract_last_command_from_gitlab_logs():
    """Test that extract_last_command_from_gitlab_logs extracts the last command correctly."""
    log = (
        "$ first command\nfirst output\n"
        "$ second command\nsecond output\n<<< step_script\n"
        "$ third command\nthird output\nExit code 1"
    )

    result = extract_last_command_from_gitlab_logs(log)

    assert "$ third command" in result
    assert "third output" in result
    assert "Exit code 1" in result
    assert "first command" not in result
    assert "second command" not in result


def test_extract_last_command_empty_log():
    """Test that extract_last_command_from_gitlab_logs handles empty logs."""
    result = extract_last_command_from_gitlab_logs("")
    assert result == ""
