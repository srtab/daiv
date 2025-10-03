from unittest.mock import MagicMock, patch

from django.http import HttpRequest

import pytest
from pydantic import SecretStr

from codebase.gitlab.api.security import validate_gitlab_webhook


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
    with patch("codebase.api.security.settings") as mock_settings:
        mock_settings.GITLAB_WEBHOOK_SECRET = SecretStr("test_secret") if secret_configured else None

        if token_header:
            mock_request.headers["X-Gitlab-Token"] = "test_secret"

        # Execute
        result = validate_gitlab_webhook(mock_request)

        # Assert
        assert result == expected_result
