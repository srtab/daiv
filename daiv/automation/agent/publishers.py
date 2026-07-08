from __future__ import annotations

import logging
from abc import abstractmethod
from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, urlencode

from django.template.loader import render_to_string

from asgiref.sync import sync_to_async

from automation.agent.git_utils import open_git_manager
from automation.agent.utils import build_langsmith_config
from codebase.base import GitPlatform, MergeRequest, Scope
from codebase.clients import RepoClient
from codebase.utils import redact_diff_content
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_NAME
from core.site_settings import site_settings

from .diff_to_metadata.graph import create_diff_to_metadata_graph

if TYPE_CHECKING:
    from automation.agent.middlewares.file_system import SandboxFileBackend
    from codebase.clients.base import GitAuthEnv
    from codebase.context import RuntimeCtx


logger = logging.getLogger("daiv.tools")


@dataclass(frozen=True)
class PublishOutcome:
    """Result of a publish attempt.

    ``published`` is True when this turn committed/pushed/created/updated; False when there was
    nothing new (no changes at all, or a clean tree already on its MR). ``merge_request`` is the MR
    to surface in state (``None`` only when there was nothing at all). ``protected_branch_fallback_source``
    is the original MR's source branch when publish fell back to a fresh MR because that branch was
    protected on the remote (``None`` otherwise); consumed by managers to bundle a notice into the reply.
    """

    merge_request: MergeRequest | None
    published: bool
    protected_branch_fallback_source: str | None = None


class ChangePublisher:
    """
    Publisher for changes made by the agent.
    """

    def __init__(self, ctx: RuntimeCtx, *, sandbox_backend: SandboxFileBackend | None = None):
        self.ctx = ctx
        self.client = RepoClient.create_instance()
        self.sandbox_backend = sandbox_backend

    @abstractmethod
    async def publish(self, **kwargs) -> Any:
        """
        Publish the changes.
        """


class GitChangePublisher(ChangePublisher):
    """
    Publisher for changes made by the agent to the Git repository.
    """

    async def publish(
        self, *, merge_request: MergeRequest | None = None, skip_ci: bool = False, as_draft: bool = False, **kwargs
    ) -> PublishOutcome:
        """
        Daiv-direct publish: ensure the run's changes reach a merge request.

        Computes one ``status_snapshot`` and decides whether anything is new (folding the former
        ``GitMiddleware._is_unpublished`` gate): a clean tree whose work is already on its MR — or no
        changes at all — short-circuits without an LLM metadata call or a no-op push. Otherwise
        commits any uncommitted work (LLM-generated message), pushes, and opens/updates the MR.
        """
        protected_branch_fallback_source: str | None = None
        default_branch = cast("str", self.ctx.config.default_branch)

        # Local-mode git (sandbox-disabled runs) pushes from the DAIV-container clone, whose
        # .git/config deliberately holds no credential — overlay the per-run credential on its git
        # subprocesses. Sandbox runs skip the lookup: in-sandbox git authenticates via the egress
        # proxy, and (on GitHub) the lookup mints a token via a platform API call.
        auth_env: GitAuthEnv | None = None
        if self.sandbox_backend is None:
            auth_env = await sync_to_async(self.client.get_git_auth_env)(self.ctx.repository)

        async with open_git_manager(
            sandbox_backend=self.sandbox_backend, gitrepo=self.ctx.gitrepo, auth_env=auth_env
        ) as git_manager:
            snapshot = await git_manager.status_snapshot(
                base_branch=default_branch,
                mr_source_branch=merge_request.source_branch if merge_request is not None else None,
            )

            if not snapshot.dirty:
                if not snapshot.diff.strip():
                    logger.info("No changes to publish.")
                    return PublishOutcome(merge_request=None, published=False)
                if merge_request is not None and not snapshot.has_unpushed:
                    logger.info("Changes already on MR !%s; nothing new.", merge_request.merge_request_id)
                    return PublishOutcome(merge_request=merge_request, published=False)

            fallback_from_mr: MergeRequest | None = None
            if merge_request is not None and await sync_to_async(self.client.is_branch_protected)(
                self.ctx.repository.slug, merge_request.source_branch
            ):
                logger.warning(
                    "Source branch '%s' of MR !%s is protected; opening a new MR with a fresh branch instead.",
                    merge_request.source_branch,
                    merge_request.merge_request_id,
                )
                fallback_from_mr = merge_request
                protected_branch_fallback_source = merge_request.source_branch
                merge_request = None

            pr_metadata_diff = (
                snapshot.diff if merge_request is None or (merge_request.draft and as_draft is False) else None
            )
            changes_metadata = await self._diff_to_metadata(
                pr_metadata_diff=pr_metadata_diff, commit_message_diff=snapshot.diff
            )

            if snapshot.dirty:
                commit_message = changes_metadata["commit_message"].commit_message
                await git_manager.commit_all(f"[skip ci] {commit_message}" if skip_ci else commit_message)

            if merge_request is None:
                branch_name = git_manager.unique_branch_name(
                    changes_metadata["pr_metadata"].branch, snapshot.remote_branches
                )
            else:
                branch_name = merge_request.source_branch

            # Only an existing MR's source branch may have advanced under the run (a dependabot
            # force-push, or a concurrent push) — integrate + retry there so the work isn't lost.
            # A fresh, unique branch can't, so leave integration off for new MRs.
            await git_manager.push_head_to(branch_name, integrate_on_reject=merge_request is not None)

        logger.info("Published changes to branch: '%s' [skip_ci: %s]", branch_name, skip_ci)

        if merge_request is None:
            merge_request = await self._create_merge_request(
                branch_name,
                changes_metadata["pr_metadata"].title,
                changes_metadata["pr_metadata"].description,
                as_draft=as_draft,
                fallback_from_mr=fallback_from_mr,
            )
            logger.info(
                "Created merge request: %s [merge_request_id: %s, draft: %r]",
                merge_request.web_url,
                merge_request.merge_request_id,
                merge_request.draft,
            )
            await self._suggest_context_file(merge_request)
        elif merge_request.draft and as_draft is False:
            merge_request = await sync_to_async(self.client.update_merge_request)(
                merge_request.repo_id, merge_request.merge_request_id, as_draft=as_draft
            )
            logger.info(
                "Updated merge request: %s [merge_request_id: %s, draft: %r]",
                merge_request.web_url,
                merge_request.merge_request_id,
                merge_request.draft,
            )

        return PublishOutcome(
            merge_request=merge_request,
            published=True,
            protected_branch_fallback_source=protected_branch_fallback_source,
        )

    async def _diff_to_metadata(self, commit_message_diff: str, pr_metadata_diff: str | None = None) -> dict[str, Any]:
        """
        Get the PR metadata from the diff.

        Args:
            ctx: The runtime context.
            commit_message_diff: The diff of the commit message.
            pr_metadata_diff: The diff of the PR metadata. If None, the PR metadata will not be computed.

        Returns:
            The pull request metadata and commit message.
        """

        input_data = {
            "commit_message_diff": redact_diff_content(commit_message_diff, self.ctx.config.omit_content_patterns)
        }
        if self.ctx.scope == Scope.ISSUE:
            input_data["extra_context"] = dedent(
                """\
                This changes were made to address the following issue:

                Issue ID: {issue.iid}
                Issue title: {issue.title}
                Issue description: {issue.description}
                """
            ).format(issue=self.ctx.issue)

        if pr_metadata_diff:
            input_data["pr_metadata_diff"] = redact_diff_content(
                pr_metadata_diff, self.ctx.config.omit_content_patterns
            )

        changes_metadata_graph = create_diff_to_metadata_graph(ctx=self.ctx, include_pr_metadata=bool(pr_metadata_diff))
        config = build_langsmith_config(
            self.ctx, trigger="diff_to_metadata", model=self.ctx.config.models.diff_to_metadata.model
        )
        result = await changes_metadata_graph.ainvoke(input_data, config=config)
        if result and ("pr_metadata" in result or "commit_message" in result):
            return result

        raise ValueError("Failed to get PR metadata from the diff.")

    async def _create_merge_request(
        self,
        branch_name: str,
        title: str,
        description: str,
        as_draft: bool = False,
        fallback_from_mr: MergeRequest | None = None,
    ) -> MergeRequest:
        """
        Update or create the merge request.

        Args:
            branch_name: The branch name.
            title: The title of the merge request.
            description: The description of the merge request.
            as_draft: Whether to create the merge request as a draft.
            fallback_from_mr: The original MR whose protected source branch forced this
                fresh MR. When provided, the description back-links to it so reviewers
                can trace the relationship.

        Returns:
            The merge request.
        """
        assignee_id = None

        if self.ctx.issue and self.ctx.issue.assignee:
            assignee_id = (
                self.ctx.issue.assignee.id
                if self.ctx.git_platform == GitPlatform.GITLAB
                else self.ctx.issue.assignee.username
            )

        return await sync_to_async(self.client.update_or_create_merge_request)(
            repo_id=self.ctx.repository.slug,
            source_branch=branch_name,
            target_branch=cast("str", self.ctx.config.default_branch),
            labels=[BOT_LABEL],
            title=title,
            assignee_id=assignee_id,
            as_draft=as_draft,
            description=render_to_string(
                "automation/issue_merge_request.txt",
                {
                    "description": description,
                    "source_repo_id": self.ctx.repository.slug,
                    "issue_id": self.ctx.issue.iid if self.ctx.issue else None,
                    "bot_name": BOT_NAME,
                    "bot_username": self.ctx.bot_username,
                    "is_gitlab": self.ctx.git_platform == GitPlatform.GITLAB,
                    "fallback_from_mr": fallback_from_mr,
                },
            ),
        )

    async def _suggest_context_file(self, merge_request: MergeRequest) -> None:
        if not site_settings.suggest_context_file_enabled or not self.ctx.config.suggest_context_file:
            return

        context_file_name = self.ctx.config.context_file_name
        if not context_file_name:
            return

        try:
            existing = await sync_to_async(self.client.get_repository_file)(
                self.ctx.repository.slug, context_file_name, ref=cast("str", self.ctx.config.default_branch)
            )
            if existing is not None:
                return

            issue_url = self._build_issue_creation_url(context_file_name)
            comment_body = render_to_string(
                "automation/suggest_context_file.txt",
                {"context_file_name": context_file_name, "bot_name": BOT_NAME, "issue_url": issue_url},
            )
            await sync_to_async(self.client.create_merge_request_comment)(
                self.ctx.repository.slug, merge_request.merge_request_id, comment_body
            )
            logger.info(
                "Suggested %s for %s MR #%s",
                context_file_name,
                self.ctx.repository.slug,
                merge_request.merge_request_id,
            )
        except Exception:
            logger.warning(
                "Failed to suggest %s for %s MR #%s",
                context_file_name,
                self.ctx.repository.slug,
                merge_request.merge_request_id,
                exc_info=True,
            )

    def _build_issue_creation_url(self, context_file_name: str) -> str:
        """
        Build a platform-specific URL that pre-fills the new-issue form.
        The issue body is kept minimal so the /init skill handles the details.
        """
        title = f"Add `{context_file_name}` to the repository"
        body = f"Create an `{context_file_name}` file for this repository."

        html_url = self.ctx.repository.html_url

        if self.ctx.git_platform == GitPlatform.GITLAB:
            params = urlencode(
                {"issue[title]": title, "issue[description]": f"{body}\n\n/label ~{BOT_AUTO_LABEL}\n"}, quote_via=quote
            )
            return f"{html_url}/-/issues/new?{params}"

        params = urlencode({"title": title, "body": body, "labels": BOT_AUTO_LABEL}, quote_via=quote)
        return f"{html_url}/issues/new?{params}"
