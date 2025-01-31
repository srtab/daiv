import logging

from ninja import Router

from .callbacks import UnprocessableEntityResponse
from .callbacks_gitlab import IssueCallback, NoteCallback, PipelineStatusCallback, PushCallback

logger = logging.getLogger("daiv.webhooks")

router = Router(tags=["codebase"])


@router.post("/callbacks/gitlab/", response={204: None, 423: UnprocessableEntityResponse})
async def gitlab_callback(request, payload: IssueCallback | NoteCallback | PushCallback | PipelineStatusCallback):
    """
    GitLab callback endpoint for processing callbacks.
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
