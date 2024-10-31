import logging

from ninja import Router

from .callbacks import UnprocessableEntityResponse
from .callbacks_gitlab import IssueCallback as GitLabIssueCallback, NoteCallback as GitLabNoteCallback, PushCallback as GitLabPushCallback
from .callbacks_github import IssueCallback as GitHubIssueCallback, NoteCallback as GitHubNoteCallback, PushCallback as GitHubPushCallback

logger = logging.getLogger(__name__)

router = Router()


@router.post("/callbacks/gitlab/", response={204: None, 423: UnprocessableEntityResponse})
async def gitlab_callback(request, payload: GitLabIssueCallback | GitLabNoteCallback | GitLabPushCallback):
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


@router.post("/callbacks/github/", response={204: None, 423: UnprocessableEntityResponse})
async def github_callback(request, payload: GitHubIssueCallback | GitHubNoteCallback | GitHubPushCallback):
    """
    GitHub callback endpoint for processing callbacks.
    """
    if payload.accept_callback():
        logger.info("GitHub Hook: Processing hook '%s' for repository %d", payload.action, payload.repository.id)
        await payload.process_callback()
    else:
        logger.info(
            "GitHub Hook: Ignored hook '%s' for repository %d, conditions for acceptance not met.",
            payload.action,
            payload.repository.id,
        )
    return 204, None
