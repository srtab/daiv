import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest
import requests
from github import Github
from github.Requester import Requester

from codebase.clients.github.utils import (
    PerThreadHTTPConnection,
    PerThreadHTTPSConnection,
    extract_last_command_from_github_logs,
    strip_iso_timestamps,
    use_per_thread_connections,
)


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


def test_strip_iso_timestamps_tab_prefixed():
    """Test that strip_iso_timestamps cleans tab-prefixed GitHub logs."""
    raw_log = (
        "Analyze (actions)\tUNKNOWN STEP\t\ufeff2026-01-31T01:00:49.2161896Z "
        "Current runner version: '2.331.0'\n"
        "Analyze (actions)\tUNKNOWN STEP\t2026-01-31T01:00:49.2183140Z ##[group]Runner Image Provisioner\n"
        "Analyze (actions)\tUNKNOWN STEP\t2026-01-31T01:00:49.2183949Z Hosted Compute Agent\n"
        "Analyze (actions)\tUNKNOWN STEP\t2026-01-31T01:00:49.2184972Z Version: 20260123.484\n"
        "Analyze (actions)\tUNKNOWN STEP\t2026-01-31T01:00:49.2185578Z Commit: "
        "6bd6555ca37d84114959e1c76d2c01448ff61c5d\n"
        "Analyze (actions)\tUNKNOWN STEP\t2026-01-31T01:00:49.2186311Z Build Date: 2026-01-23T19:41:17Z\n"
    )

    result = strip_iso_timestamps(raw_log)

    assert "Analyze (actions)" not in result
    assert "UNKNOWN STEP" not in result
    assert "Current runner version: '2.331.0'" in result
    assert "##[group]Runner Image Provisioner" in result
    assert "Hosted Compute Agent" in result
    assert "Version: 20260123.484" in result
    assert "Commit: 6bd6555ca37d84114959e1c76d2c01448ff61c5d" in result
    assert "Build Date: 2026-01-23T19:41:17Z" in result


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


@pytest.mark.parametrize("connection_class", [PerThreadHTTPConnection, PerThreadHTTPSConnection])
def test_threads_sharing_a_connection_each_send_their_own_request(connection_class):
    connection = connection_class("api.github.com")
    both_requested = threading.Barrier(2, timeout=5)

    def call(path):
        connection.request("GET", path, None, {})
        both_requested.wait()
        return connection.getresponse().read()

    def echo(session, url, **kwargs):
        return Mock(status_code=200, headers={}, text=url)

    with patch.object(requests.Session, "get", new=echo), ThreadPoolExecutor(2) as pool:
        first, second = pool.map(call, ["/first", "/second"])

    assert (first.endswith("/first"), second.endswith("/second")) == (True, True)


def test_requesters_built_after_the_switch_connect_per_thread(monkeypatch):
    for name in ("_Requester__httpConnectionClass", "_Requester__httpsConnectionClass"):
        monkeypatch.setattr(Requester, name, getattr(Requester, name))

    use_per_thread_connections()

    assert Github().requester._Requester__connectionClass is PerThreadHTTPSConnection  # noqa: SLF001
