import logging
from typing import TYPE_CHECKING

from ninja import Router
from ninja.errors import HttpError

from codebase.api.router import router
from codebase.base import GitPlatform
from codebase.conf import settings
from webhooks.callbacks import UnprocessableEntityResponse

from .callbacks import IssueCallback, MergeRequestCallback, NoteCallback, PipelineCallback, PushCallback  # noqa: TC001
from .security import validate_gitlab_webhook

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger("daiv.webhooks")


def _authenticate(request: HttpRequest) -> bool:
    """
    Answer 403 when the client type is not GitLab, else check the token. ninja runs this before parsing the body.
    """
    if settings.CLIENT != GitPlatform.GITLAB:
        logger.warning("GitLab Hook: Client type is not set to GitLab, skipping callback.")
        raise HttpError(403, "Client type is not set to GitLab")
    return validate_gitlab_webhook(request)


gitlab_router = Router(auth=_authenticate)
router.add_router("", gitlab_router)

_RESPONSES = {204: None, 401: None, 403: None, 422: UnprocessableEntityResponse}


@gitlab_router.post("/callbacks/gitlab", response=_RESPONSES)
@gitlab_router.post("/callbacks/gitlab/", response=_RESPONSES, url_name="gitlab_callback")
async def callback(
    request, payload: IssueCallback | MergeRequestCallback | NoteCallback | PushCallback | PipelineCallback
):
    """
    GitLab callback endpoint for processing callbacks; the router's ``_authenticate`` has already passed.
    """
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
