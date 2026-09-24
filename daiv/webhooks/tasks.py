import logging
from typing import TYPE_CHECKING

from django_tasks import task
from sessions.services import aget_session_ref

from codebase.clients import RepoClient
from codebase.exceptions import CloneRefNotFoundError

if TYPE_CHECKING:
    from automation.agent.results import AgentResult
    from codebase.base import MergeRequest

logger = logging.getLogger("daiv.tasks")


def _mr_comment_skip_result(response: str, merge_request: MergeRequest) -> AgentResult:
    from automation.agent.results import AgentResult

    return AgentResult(
        response=response,
        code_changes=False,
        merge_request_id=merge_request.merge_request_id,
        merge_request_web_url=merge_request.web_url,
        usage=None,
    )


@task(dedup=True)
async def address_issue_task(
    repo_id: str,
    issue_iid: int,
    mention_comment_id: str | None = None,
    ref: str | None = None,
    thread_id: str | None = None,
    sandbox_environment_id: str | None = None,
) -> AgentResult | None:
    """
    Address an issue by creating a merge request with the changes described on the issue description.

    Args:
        repo_id (str): The repository id.
        issue_iid (int): The issue id.
        mention_comment_id (str | None): The mention comment id. Defaults to None.
        ref (str | None): The ref to clone. Defaults to the session's working branch, else the repository default.
        thread_id (str | None): The LangGraph checkpoint key minted by the caller. When ``None``
            the manager computes the deterministic id from the repository and the issue iid.
        sandbox_environment_id (str | None): Per-run sandbox env id resolved at webhook time.
            When ``None``, ``set_runtime_ctx`` auto-resolves via
            :func:`sandbox_envs.services.resolve_env_for_run` (USER tier skipped) and ultimately
            falls back to the GLOBAL ``is_default=True`` env — so a non-None env may still apply.
    """
    from webhooks.managers.issue_addressor import IssueAddressorManager

    client = RepoClient.create_instance()
    issue = client.get_issue(repo_id, issue_iid)
    # Unguarded on purpose: degrading a failed read to "" re-clones the default branch, which is the exact loss
    # ``Session.ref`` exists to prevent.
    effective_ref = ref or (await aget_session_ref(thread_id=thread_id) if thread_id else "")
    return await IssueAddressorManager.address_issue(
        repo_id=repo_id,
        issue=issue,
        mention_comment_id=mention_comment_id,
        ref=effective_ref or None,
        thread_id=thread_id,
        sandbox_env_id=sandbox_environment_id,
    )


@task(dedup=True)
async def address_mr_comments_task(
    repo_id: str,
    merge_request_id: int,
    mention_comment_id: str,
    thread_id: str | None = None,
    sandbox_environment_id: str | None = None,
) -> AgentResult:
    """
    Address comments left directly on the merge request (not in the diff or thread) that mention DAIV.

    Args:
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        mention_comment_id (str): The mention comment id.
        thread_id (str | None): The LangGraph checkpoint key minted by the caller. When ``None``
            the manager computes the deterministic id from the repository and the merge request iid.
        sandbox_environment_id (str | None): Per-run sandbox env id resolved at webhook time.
            When ``None``, ``set_runtime_ctx`` auto-resolves via
            :func:`sandbox_envs.services.resolve_env_for_run` (USER tier skipped) and ultimately
            falls back to the GLOBAL ``is_default=True`` env — so a non-None env may still apply.
    """
    from webhooks.managers.review_addressor import CommentsAddressorManager

    client = RepoClient.create_instance()
    merge_request = client.get_merge_request(repo_id, merge_request_id)

    if merge_request.merged:
        response = (
            f"This merge request has already been merged, so I can't act on comments left against "
            f"its branch (`{merge_request.source_branch}`). Please open a new issue or merge request "
            f"for any follow-up changes."
        )
        logger.warning("Skipping MR-comment run for %s!%s: already merged.", repo_id, merge_request_id)
        client.create_merge_request_comment(repo_id, merge_request_id, body=response)
        return _mr_comment_skip_result(response, merge_request)

    try:
        return await CommentsAddressorManager.address_comments(
            repo_id=repo_id,
            merge_request=merge_request,
            mention_comment_id=mention_comment_id,
            thread_id=thread_id,
            sandbox_env_id=sandbox_environment_id,
        )
    except CloneRefNotFoundError:
        response = (
            f"The source branch `{merge_request.source_branch}` for this merge request no longer "
            f"exists, so I can't check it out to address this comment. If it was merged and deleted, "
            f"please open a new issue or merge request for any follow-up changes."
        )
        logger.warning(
            "Skipping MR-comment run for %s!%s: source branch %r no longer exists.",
            repo_id,
            merge_request_id,
            merge_request.source_branch,
        )
        client.create_merge_request_comment(repo_id, merge_request_id, body=response)
        return _mr_comment_skip_result(response, merge_request)
