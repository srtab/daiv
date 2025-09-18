import logging

from codebase.api.callbacks import UnprocessableEntityResponse
from codebase.api.router import router

from .callbacks import IssueCallback, IssueCommentCallback, PushCallback
from .security import validate_github_webhook

logger = logging.getLogger("daiv.webhooks")


@router.post("/callbacks/github/", response={204: None, 401: None, 423: UnprocessableEntityResponse})
async def callback(request, payload: IssueCallback | IssueCommentCallback | PushCallback):
    """
    GitHub callback endpoint for processing callbacks.
    """
    event = request.headers.get("X-GitHub-Event")

    if not validate_github_webhook(request):
        logger.warning("GitHub Hook: Unauthorized webhook '%s' for project '%s'", event, payload.repository.full_name)
        return 401, None

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
