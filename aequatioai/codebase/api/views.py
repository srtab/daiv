import logging

from ninja import Router

from .webhooks_gitlab import IssueWebHook, NoteWebHook, PushWebHook

logger = logging.getLogger(__name__)

router = Router()


@router.get("/webhooks/gitlab/", response={204: None})
def gitlab_webhook(request, payload: IssueWebHook | NoteWebHook | PushWebHook):
    if payload.accept_webhook():
        logger.info("GitLab Hook: Processing hook '%s', conditions for acceptance not met.", payload.object_kind)
        payload.process_webhook()
    else:
        logger.info("GitLab Hook: Ignored hook '%s', conditions for acceptance not met.", payload.object_kind)
    return 204, None
