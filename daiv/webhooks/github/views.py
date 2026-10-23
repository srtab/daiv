import logging
from typing import TYPE_CHECKING

from ninja import Router
from ninja.errors import HttpError

from codebase.base import GitPlatform
from codebase.conf import settings
from webhooks.callbacks import CALLBACK_RESPONSES

from .callbacks import IssueCallback, IssueCommentCallback, PullRequestCallback, PushCallback, WorkflowRunCallback  # noqa: TC001
from .security import validate_github_webhook

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger("daiv.webhooks")


def _authenticate(request: HttpRequest) -> bool:
    """
    Answer 403 when the client type is not GitHub, else check the signature. ninja runs this before parsing the body.
    """
    if settings.CLIENT != GitPlatform.GITHUB:
        logger.warning("GitHub Hook: Client type is not set to GitHub, skipping callback.")
        raise HttpError(403, "Client type is not set to GitHub")
    return validate_github_webhook(request)


router = Router(auth=_authenticate)


@router.post("/github", response=CALLBACK_RESPONSES)
@router.post("/github/", response=CALLBACK_RESPONSES)
async def callback(
    request, payload: IssueCallback | IssueCommentCallback | PullRequestCallback | PushCallback | WorkflowRunCallback
):
    """
    GitHub callback endpoint for processing callbacks; the router's ``_authenticate`` has already passed.
    """
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
