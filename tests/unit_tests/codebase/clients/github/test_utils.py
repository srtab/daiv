from codebase.clients.github.utils import extract_last_command_from_github_logs, strip_iso_timestamps


def test_strip_iso_timestamps():
    """Test that strip_iso_timestamps properly cleans the timestamps from GitHub logs."""
    raw_log = (
        "2025-09-22T21:40:35.4116534Z Current runner version: '2.328.0'\n"
        "2025-09-22T21:40:35.4147892Z ##[group]Runner Image Provisioner\n"
        "2025-09-22T21:40:35.4148983Z Hosted Compute Agent\n"
        "2025-09-22T21:40:35.4149811Z Version: 20250829.383\n"
        "2025-09-22T21:40:35.4151020Z Commit: 27cb235aab5b0e52e153a26cd86b4742e89dac5d\n"
        "2025-09-22T21:40:35.4152126Z Build Date: 2025-08-29T13:48:48Z\n"
        "2025-09-22T21:40:35.4153008Z ##[endgroup]\n"
    )

    result = strip_iso_timestamps(raw_log)

    assert "Current runner version: '2.328.0'" in result
    assert "##[group]Runner Image Provisioner" in result
    assert "Hosted Compute Agent" in result
    assert "Version: 20250829.383" in result
    assert "Commit: 27cb235aab5b0e52e153a26cd86b4742e89dac5d" in result
    assert "Build Date: 2025-08-29T13:48:48Z" in result
    assert "##[endgroup]" in result


def test_extract_last_command_from_github_logs():
    """Test that extract_last_command_from_github_logs extracts the last command correctly."""
    log = (
        "##[group]Runner Image Provisioner\n"
        "Hosted Compute Agent\n"
        "Version: 20250829.383\n"
        "Commit: 27cb235aab5b0e52e153a26cd86b4742e89dac5d\n"
        "Build Date: 2025-08-29T13:48:48Z\n"
        "##[endgroup]\n"
        "##[group]Operating System\n"
        "Ubuntu\n"
        "24.04.3\n"
        "##[endgroup]\n"
        "##[group]Run make test\n"
        "make test\n"
        "Test output in group\n"
        "##[endgroup]\n"
        "Test output outside group\n"
        "Test output 2 outside group\n"
        "##[error]Process completed with exit code 1.\n"
    )

    result = extract_last_command_from_github_logs(log)

    assert "make test" in result
    assert "Test output in group" in result
    assert "##[endgroup]" in result
    assert "Test output outside group" in result
    assert "Test output 2 outside group" in result
    assert "##[error] Process completed with exit code 1." not in result


def test_extract_last_command_empty_log():
    """Test that _extract_last_command_from_github_logs handles empty logs."""
    result = extract_last_command_from_github_logs("")
    assert result == ""
