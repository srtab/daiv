import logging

from codebase.api.router import router
from codebase.base import GitPlatform
from codebase.conf import settings
from webhooks.callbacks import UnprocessableEntityResponse

from .callbacks import IssueCallback, IssueCommentCallback, PullRequestCallback, PushCallback, WorkflowRunCallback  # noqa: TC001
from .security import validate_github_webhook

logger = logging.getLogger("daiv.webhooks")


@router.post(
    "/callbacks/github",
    response={204: None, 401: None, 403: None, 422: UnprocessableEntityResponse},
    auth=validate_github_webhook,
)
@router.post(
    "/callbacks/github/",
    response={204: None, 401: None, 403: None, 422: UnprocessableEntityResponse},
    auth=validate_github_webhook,
)
async def callback(
    request, payload: IssueCallback | IssueCommentCallback | PullRequestCallback | PushCallback | WorkflowRunCallback
):
    """
    GitHub callback endpoint for processing callbacks.

    The signature is verified (``auth``) before the payload is parsed; a request that fails it gets 401 Unauthorized.
    Returns 403 Forbidden if client type is not set to GitHub.
    """
    if settings.CLIENT != GitPlatform.GITHUB:
        logger.warning("GitHub Hook: Client type is not set to GitHub, skipping callback.")
        return 403, None

    event = request.headers.get("X-GitHub-Event")

    if payload.accept_callback():
        logger.info("GitHub Hook: Processing hook '%s' for project '%s'", event, payload.repository.full_name)
        await payload.process_callback()
    else:
        logger.info(
            "GitHub Hook: Ignored hook '%s' for project '%s', conditions for acceptance not met.",
            event,
            payload.repository.full_name,
        )
    return 204, None
