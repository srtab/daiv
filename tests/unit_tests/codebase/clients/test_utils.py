from codebase.clients.utils import _clean_ansi_codes


def test__clean_ansi_codes():
    """Test that _clean_gitlab_logs properly cleans GitLab logs."""
    raw_log = (
        "\x1b[0msection_start:123: step_script\r\n"
        "Running command\x1b[0m\r\n"
        "Output with\rcarriage return\r\n"
        "\x1b[32mColored text\x1b[0m\n"
        "section_end:123: step_script"
    )

    result = _clean_ansi_codes(raw_log)

    assert "\x1b[" not in result  # No ANSI codes
    assert "\r\n" not in result  # No Windows line endings


def test__clean_ansi_codes_empty_log():
    """Test that _clean_ansi_codes handles empty logs."""
    assert _clean_ansi_codes("") == ""
