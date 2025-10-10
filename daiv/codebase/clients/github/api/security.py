import hmac
import logging
from hashlib import sha256
from typing import TYPE_CHECKING

from codebase.conf import settings

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger("daiv.webhooks")


def validate_github_webhook(request: HttpRequest) -> bool:
    """
    Validate GitHub webhook by computing an HMAC SHA256 hash of the request body
    using the configured secret and comparing it to the X-Hub-Signature-256 header.

    Args:
        request: The HTTP request containing the webhook payload

    Returns:
        True if the webhook is valid, False otherwise
    """
    if not settings.GITHUB_WEBHOOK_SECRET:
        logger.warning("GitHub webhook validation skipped: No secret token configured")
        return True

    signature = request.headers.get("X-Hub-Signature-256")
    if not signature:
        logger.warning("GitHub webhook validation failed: Missing X-Hub-Signature-256 header")
        return False

    if not signature.startswith("sha256="):
        logger.warning("GitHub webhook validation failed: Invalid signature format")
        return False

    # Get the signature without the 'sha256=' prefix
    signature = signature[7:]

    # Compute the HMAC SHA256 hash of the request body
    mac = hmac.new(settings.GITHUB_WEBHOOK_SECRET.get_secret_value().encode(), msg=request.body, digestmod=sha256)
    expected_signature = mac.hexdigest()

    # Use constant-time comparison to prevent timing attacks
    is_valid = hmac.compare_digest(signature, expected_signature)

    if is_valid:
        logger.debug("GitHub webhook validation successful")
    else:
        logger.warning("GitHub webhook validation failed: Invalid signature")

    return is_valid
