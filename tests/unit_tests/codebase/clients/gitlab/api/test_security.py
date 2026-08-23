from unittest.mock import MagicMock, patch

from django.http import HttpRequest

import pytest
from pydantic import SecretStr

from codebase.clients.gitlab.api.security import validate_gitlab_webhook


@pytest.fixture
def mock_request():
    request = MagicMock(spec=HttpRequest)
    request.headers = {}
    request.body = b'{"test": "data"}'
    return request


@pytest.mark.parametrize(
    "secret_configured,token_header,expected_result",
    [
        (True, True, True),  # Secret configured, token header present and valid
        (True, False, False),  # Secret configured, token header missing
        (False, True, True),  # No secret configured, token header present (should pass)
        (False, False, True),  # No secret configured, token header missing (should pass)
    ],
)
def test_validate_gitlab_webhook(mock_request, secret_configured, token_header, expected_result):
    """Test GitLab webhook validation with various scenarios."""
    # Setup
    with patch("codebase.clients.gitlab.api.security.settings") as mock_settings:
        mock_settings.GITLAB_WEBHOOK_SECRET = SecretStr("test_secret") if secret_configured else None

        if token_header:
            mock_request.headers["X-Gitlab-Token"] = "test_secret"

        # Execute
        result = validate_gitlab_webhook(mock_request)

        # Assert
        assert result == expected_result


def test_a_non_ascii_token_is_rejected_rather_than_raising(mock_request):
    """A non-ASCII token must return False, not raise.

    ``hmac.compare_digest`` raises ``TypeError`` on a ``str`` holding non-ASCII, and header values
    reach Django latin-1-decoded — so comparing as ``str`` turns an attacker-supplied byte into a
    500 (and a Sentry event) on an unauthenticated endpoint instead of the intended 401.
    """
    with patch("codebase.clients.gitlab.api.security.settings") as mock_settings:
        mock_settings.GITLAB_WEBHOOK_SECRET = SecretStr("test_secret")
        mock_request.headers["X-Gitlab-Token"] = b"\xe9".decode("latin-1")

        assert validate_gitlab_webhook(mock_request) is False
