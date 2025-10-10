import hmac
import logging
from typing import TYPE_CHECKING

from codebase.conf import settings

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger("daiv.webhooks")


def validate_gitlab_webhook(request: HttpRequest) -> bool:
    """
    Validate GitLab webhook by checking the X-Gitlab-Token header against the configured secret.

    Args:
        request: The HTTP request containing the webhook payload

    Returns:
        True if the webhook is valid, False otherwise
    """
    if not settings.GITLAB_WEBHOOK_SECRET:
        logger.warning("GitLab webhook validation skipped: No secret token configured")
        return True

    token = request.headers.get("X-Gitlab-Token")
    if not token:
        logger.warning("GitLab webhook validation failed: Missing X-Gitlab-Token header")
        return False

    # Use constant-time comparison to prevent timing attacks
    is_valid = hmac.compare_digest(token, settings.GITLAB_WEBHOOK_SECRET.get_secret_value())

    if is_valid:
        logger.debug("GitLab webhook validation successful")
    else:
        logger.warning("GitLab webhook validation failed: Invalid token")

    return is_valid
