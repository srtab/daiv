from unittest.mock import MagicMock, patch

from django.http import HttpRequest

import pytest
from pydantic import SecretStr

from codebase.github.api.security import validate_github_webhook


@pytest.fixture
def mock_request():
    request = MagicMock(spec=HttpRequest)
    request.headers = {}
    request.body = b'{"test": "data"}'
    return request


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
        mock_settings.GITHUB_WEBHOOK_SECRET = SecretStr("test_secret") if secret_configured else None

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
