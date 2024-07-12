import logging

from ninja import Router

from .webhooks import UnprocessableEntityResponse
from .webhooks_gitlab import IssueWebHook, NoteWebHook, PushWebHook

logger = logging.getLogger(__name__)

router = Router()


@router.post("/webhooks/gitlab/", response={204: None, 423: UnprocessableEntityResponse})
def gitlab_webhook(request, payload: IssueWebHook | NoteWebHook | PushWebHook):
    """
    GitLab webhook endpoint for processing webhooks.
    """
    if payload.accept_webhook():
        logger.info("GitLab Hook: Processing hook '%s' for project %d", payload.object_kind, payload.project.id)
        payload.process_webhook()
    else:
        logger.info(
            "GitLab Hook: Ignored hook '%s' for project %d, conditions for acceptance not met.",
            payload.object_kind,
            payload.project.id,
        )
    return 204, None
