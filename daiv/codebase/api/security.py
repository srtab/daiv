import hmac
import logging
from hashlib import sha256

from django.http import HttpRequest

from codebase.conf import settings

logger = logging.getLogger("daiv.webhooks")


def validate_gitlab_webhook(request: HttpRequest) -> bool:
    """
    Validate GitLab webhook by checking the X-Gitlab-Token header against the configured secret.

    Args:
        request: The HTTP request containing the webhook payload

    Returns:
        True if the webhook is valid, False otherwise
    """
    if not settings.WEBHOOK_SECRET_GITLAB:
        logger.warning("GitLab webhook validation skipped: No secret token configured")
        return True

    token = request.headers.get("X-Gitlab-Token")
    if not token:
        logger.warning("GitLab webhook validation failed: Missing X-Gitlab-Token header")
        return False

    # Use constant-time comparison to prevent timing attacks
    is_valid = hmac.compare_digest(token, settings.WEBHOOK_SECRET_GITLAB)

    if is_valid:
        logger.debug("GitLab webhook validation successful")
    else:
        logger.warning("GitLab webhook validation failed: Invalid token")

    return is_valid


def validate_github_webhook(request: HttpRequest) -> bool:
    """
    Validate GitHub webhook by computing an HMAC SHA256 hash of the request body
    using the configured secret and comparing it to the X-Hub-Signature-256 header.

    Args:
        request: The HTTP request containing the webhook payload

    Returns:
        True if the webhook is valid, False otherwise
    """
    if not settings.WEBHOOK_SECRET_GITHUB:
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
    mac = hmac.new(settings.WEBHOOK_SECRET_GITHUB.encode(), msg=request.body, digestmod=sha256)
    expected_signature = mac.hexdigest()

    # Use constant-time comparison to prevent timing attacks
    is_valid = hmac.compare_digest(signature, expected_signature)

    if is_valid:
        logger.debug("GitHub webhook validation successful")
    else:
        logger.warning("GitHub webhook validation failed: Invalid signature")

    return is_valid


def validate_webhook(request: HttpRequest) -> bool:
    """
    Validate webhook by determining the source and calling the appropriate validation function.

    Args:
        request: The HTTP request containing the webhook payload

    Returns:
        True if the webhook is valid, False otherwise
    """
    # Determine the webhook source based on headers
    if "X-Gitlab-Event" in request.headers:
        return validate_gitlab_webhook(request)
    elif "X-GitHub-Event" in request.headers:
        return validate_github_webhook(request)
    else:
        logger.warning("Webhook validation failed: Unknown webhook source")
        return False
