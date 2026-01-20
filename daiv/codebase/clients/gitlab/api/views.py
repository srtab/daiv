import logging

from codebase.api.callbacks import UnprocessableEntityResponse
from codebase.api.router import router
from codebase.base import GitPlatform
from codebase.conf import settings

from .callbacks import IssueCallback, NoteCallback, PushCallback  # noqa: TC001
from .security import validate_gitlab_webhook

logger = logging.getLogger("daiv.webhooks")


@router.post("/callbacks/gitlab", response={204: None, 401: None, 403: None, 422: UnprocessableEntityResponse})
@router.post("/callbacks/gitlab/", response={204: None, 401: None, 403: None, 422: UnprocessableEntityResponse})
async def callback(request, payload: IssueCallback | NoteCallback | PushCallback):
    """
    GitLab callback endpoint for processing callbacks.

    Validates the webhook secret before processing the callback and returns 401 Unauthorized if validation fails.
    Returns 403 Forbidden if client type is not set to GitLab.
    """
    if settings.CLIENT != GitPlatform.GITLAB:
        logger.warning("GitLab Hook: Client type is not set to GitLab, skipping callback.")
        return 403, None

    if not validate_gitlab_webhook(request):
        logger.warning("GitLab Hook: Unauthorized webhook request for project %d", payload.project.id)
        return 401, None

    if payload.accept_callback():
        logger.info("GitLab Hook: Processing hook '%s' for project %d", payload.object_kind, payload.project.id)
        await payload.process_callback()
    else:
        logger.info(
            "GitLab Hook: Ignored hook '%s' for project %d, conditions for acceptance not met.",
            payload.object_kind,
            payload.project.id,
        )
    return 204, None
