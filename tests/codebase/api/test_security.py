from unittest.mock import MagicMock, patch

from django.http import HttpRequest

import pytest

from codebase.api.security import validate_github_webhook, validate_gitlab_webhook, validate_webhook


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
        mock_settings.WEBHOOK_SECRET_GITLAB = "test_secret" if secret_configured else None

        if token_header:
            mock_request.headers["X-Gitlab-Token"] = "test_secret"

        # Execute
        result = validate_gitlab_webhook(mock_request)

        # Assert
        assert result == expected_result


@pytest.mark.parametrize(
    "secret_configured,signature_header,valid_signature,expected_result",
    [
        (True, True, True, True),  # Secret configured, signature header present and valid
        (True, True, False, False),  # Secret configured, signature header present but invalid
        (True, False, False, False),  # Secret configured, signature header missing
        (False, True, False, True),  # No secret configured, signature header present (should pass)
        (False, False, False, True),  # No secret configured, signature header missing (should pass)
    ],
)
def test_validate_github_webhook(mock_request, secret_configured, signature_header, valid_signature, expected_result):
    """Test GitHub webhook validation with various scenarios."""
    # Setup
    with patch("codebase.api.security.settings") as mock_settings:
        mock_settings.WEBHOOK_SECRET_GITHUB = "test_secret" if secret_configured else None

        if signature_header:
            # For valid signature, we need to compute the actual HMAC
            if valid_signature:
                import hmac
                from hashlib import sha256

                mac = hmac.new(b"test_secret", msg=mock_request.body, digestmod=sha256)
                signature = f"sha256={mac.hexdigest()}"
            else:
                signature = "sha256=invalid_signature"

            mock_request.headers["X-Hub-Signature-256"] = signature

        # Execute
        result = validate_github_webhook(mock_request)

        # Assert
        assert result == expected_result


def test_validate_webhook_gitlab(mock_request):
    """Test webhook validation for GitLab source."""
    # Setup
    mock_request.headers["X-Gitlab-Event"] = "Push Hook"

    with patch("codebase.api.security.validate_gitlab_webhook", return_value=True) as mock_validate:
        # Execute
        result = validate_webhook(mock_request)

        # Assert
        assert result is True
        mock_validate.assert_called_once_with(mock_request)


def test_validate_webhook_github(mock_request):
    """Test webhook validation for GitHub source."""
    # Setup
    mock_request.headers["X-GitHub-Event"] = "push"

    with patch("codebase.api.security.validate_github_webhook", return_value=True) as mock_validate:
        # Execute
        result = validate_webhook(mock_request)

        # Assert
        assert result is True
        mock_validate.assert_called_once_with(mock_request)


def test_validate_webhook_unknown_source(mock_request):
    """Test webhook validation for unknown source."""
    # Execute
    result = validate_webhook(mock_request)

    # Assert
    assert result is False
