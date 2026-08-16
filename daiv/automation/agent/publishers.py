from __future__ import annotations

import logging
from abc import abstractmethod
from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, urlencode

from django.core.exceptions import ObjectDoesNotExist
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse

from asgiref.sync import sync_to_async

from automation.agent.git_utils import open_git_manager
from automation.agent.utils import build_langsmith_config
from codebase.base import GitPlatform, MergeRequest, MergeRequestDiffStats, Scope
from codebase.clients import RepoClient
from codebase.exceptions import MergeRequestBranchNotVisibleError
from codebase.utils import diff_line_stats, redact_diff_content
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_NAME
from core.site_settings import site_settings
from core.utils import build_absolute_url

from .diff_to_metadata.graph import create_diff_to_metadata_graph

if TYPE_CHECKING:
    from automation.agent.middlewares.file_system import SandboxFileBackend
    from codebase.clients.base import GitAuthEnv
    from codebase.context import RuntimeCtx


logger = logging.getLogger("daiv.tools")

# Git trailer token carrying the producing session's URL. A wire format read by `git log`
# and `git interpret-trailers`, so it stays a literal rather than deriving from BOT_NAME.
SESSION_TRAILER = "DAIV-Session"


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
    diff_stats: MergeRequestDiffStats | None = None
    """Lines added/removed and files touched by the run's work, for the composer's ``+x −y``
    pill. ``None`` only when no publish was attempted; a publish that found nothing reports
    zeros, because the pill has to be able to go back down.

    Counted from ``status_snapshot``'s diff — the merge-base delta for the very branch the
    MR is built from, so it already *is* what the MR page shows, computed for free from a
    string that is in memory anyway. Asking the platform instead reads worse on both
    counts: GitLab has no aggregate endpoint and downloads the whole MR diff to add it up
    (truncating it past a size cap), and a freshly-created MR whose diff is still being
    prepared answers zero."""


class ChangePublisher:
    """
    Publisher for changes made by the agent.
    """

    def __init__(
        self, ctx: RuntimeCtx, *, sandbox_backend: SandboxFileBackend | None = None, thread_id: str | None = None
    ):
        self.ctx = ctx
        self.client = RepoClient.create_instance()
        self.sandbox_backend = sandbox_backend
        self.thread_id = thread_id

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
        # The diff base is the MR's real target — for a branch stacked off a release branch that is
        # the release branch, not the default. status_snapshot then diffs against the merge-base of
        # this branch and HEAD. Falls back to the default branch when there is no MR (or a blank target).
        base_branch = (
            merge_request.target_branch if merge_request is not None and merge_request.target_branch else default_branch
        )

        # Local-mode git (sandbox-disabled runs) pushes from the DAIV-container clone, whose
        # .git/config deliberately holds no credential — overlay the per-run credential on its git
        # subprocesses. Sandbox runs skip the lookup — in-sandbox git authenticates via the egress
        # proxy's platform token, minted at turn start with a platform-specific TTL (GitHub
        # installation tokens live 1h). A turn that outlives it would fail this publish's network git ops
        # (ls-remote/push) and lose the run's work, so re-mint and deliver a fresh token onto the
        # live session first. Best-effort: a failed refresh degrades to publishing with the
        # turn-start token, i.e. the pre-existing behavior.
        auth_env: GitAuthEnv | None = None
        if self.sandbox_backend is None:
            auth_env = await sync_to_async(self.client.get_git_auth_env)(self.ctx.repository)
        else:
            await self._refresh_sandbox_egress()

        async with open_git_manager(
            sandbox_backend=self.sandbox_backend, gitrepo=self.ctx.gitrepo, auth_env=auth_env
        ) as git_manager:
            snapshot = await git_manager.status_snapshot(
                base_branch=base_branch,
                mr_source_branch=merge_request.source_branch if merge_request is not None else None,
            )

            # Above the empty-diff return, not below it: see ``PublishOutcome.diff_stats``.
            diff_stats = diff_line_stats(snapshot.diff)

            if not snapshot.dirty and not snapshot.diff.strip():
                logger.info("No changes to publish.")
                return PublishOutcome(merge_request=None, published=False, diff_stats=diff_stats)

            if not snapshot.dirty and merge_request is not None and not snapshot.has_unpushed:
                logger.info("Changes already on MR !%s; nothing new.", merge_request.merge_request_id)
                return PublishOutcome(merge_request=merge_request, published=False, diff_stats=diff_stats)

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
                if skip_ci:
                    commit_message = f"[skip ci] {commit_message}"
                await git_manager.commit_all(await self._with_session_trailer(commit_message))

            if merge_request is None:
                branch_name = git_manager.unique_branch_name(
                    changes_metadata["pr_metadata"].branch, snapshot.remote_branches
                )
            else:
                branch_name = merge_request.source_branch

            # An ephemeral-token push (GitLab's project-scoped bot) yields a pipeline that can't read
            # private cross-project CI includes; skip that push's CI and re-trigger as the service
            # account below. The client capability answers False for platforms without ephemeral
            # tokens, so no platform check is needed here; an explicit skip_ci ("no CI at all") also
            # leaves nothing to heal.
            heal_pipeline = not skip_ci and await sync_to_async(self.client.push_uses_ephemeral_token)(
                self.ctx.repository
            )

            # Only an existing MR's source branch may have advanced under the run (a dependabot
            # force-push, or a concurrent push) — integrate + retry there so the work isn't lost.
            # A fresh, unique branch can't, so leave integration off for new MRs.
            # skip_ci here suppresses only the ephemeral bot's doomed pipeline (not the caller's
            # skip_ci intent); _trigger_service_account_pipeline recreates it below.
            await git_manager.push_head_to(
                branch_name, integrate_on_reject=merge_request is not None, skip_ci=heal_pipeline
            )

        logger.info("Published changes to branch: '%s' [skip_ci: %s]", branch_name, skip_ci)

        if merge_request is None:
            try:
                merge_request = await self._create_merge_request(
                    branch_name,
                    changes_metadata["pr_metadata"].title,
                    changes_metadata["pr_metadata"].description,
                    as_draft=as_draft,
                    fallback_from_mr=fallback_from_mr,
                )
            except MergeRequestBranchNotVisibleError:
                # Branch is pushed but GitLab won't open the MR yet; failing here would orphan the work
                # and discard the agent's reply, so report a partial publish (MR pending) and log loudly.
                # On the heal path the push was skip-ci'd with no MR yet to trigger against, so name the
                # suppressed pipeline in the log — the branch has no CI until the MR is created.
                pipeline_note = (
                    " The push skipped CI and no pipeline was triggered; CI will not run until the MR exists."
                    if heal_pipeline
                    else ""
                )
                logger.error(
                    "Pushed branch '%s' but GitLab did not make it visible for MR creation within the retry "
                    "budget; reporting a partial publish (MR pending).%s",
                    branch_name,
                    pipeline_note,
                )
                return PublishOutcome(
                    merge_request=None,
                    published=True,
                    protected_branch_fallback_source=protected_branch_fallback_source,
                    diff_stats=diff_stats,
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

        if heal_pipeline and merge_request is not None:
            await self._trigger_service_account_pipeline(merge_request)

        return PublishOutcome(
            merge_request=merge_request,
            published=True,
            protected_branch_fallback_source=protected_branch_fallback_source,
            diff_stats=diff_stats,
        )

    async def _refresh_sandbox_egress(self) -> None:
        """Re-mint the git-platform token and deliver it onto the live sandbox session, so the
        publish's in-sandbox network git ops (ls-remote/push) run with a fresh credential even when
        the turn outlived the token minted at turn start.

        Refreshing unconditionally before the first network op — rather than reacting to a failed
        one — keeps the recovery independent of git's auth-error wording, which varies by version and
        transport. Delivery is skipped when there is nothing to refresh (no egress proxy, a
        token-less/eval platform, or a re-mint that returned the same token — e.g. GitLab's
        day-cached clone token).

        Only called in sandbox mode. Best-effort by design — the broad ``except`` is deliberate: the
        platform mint and the sidecar PUT raise a spread of platform-/transport-specific errors the
        (platform-agnostic) publisher shouldn't enumerate, and any failure degrades cleanly to
        publishing with the turn-start token (the pre-existing behavior). The stack trace is logged
        so a persistent refresh failure stays diagnosable.
        """
        from sandbox_envs.services import refresh_platform_egress

        backend = self.sandbox_backend
        sandbox = self.ctx.sandbox
        if backend is None or sandbox is None:  # pragma: no cover - only called in sandbox mode
            return
        try:
            egress = await sync_to_async(refresh_platform_egress)(sandbox.egress, self.client, self.ctx.repository)
            # refresh_platform_egress returns the *input* object when there was nothing to swap in
            # (no proxy, token-less platform, or an unchanged token) — so identity means "nothing to
            # deliver". The explicit `is None` also narrows the `EgressConfigRequest | None` return for
            # the type checker before the non-null refresh_egress call below.
            if egress is None or egress is sandbox.egress:
                return
            await backend.refresh_egress(egress)
            logger.info("Refreshed the sandbox egress token for %s before publish", self.ctx.repository.slug)
        except Exception:
            logger.exception(
                "Could not refresh the sandbox egress token for %s before publish; proceeding with the "
                "turn-start token",
                self.ctx.repository.slug,
            )

    async def _trigger_service_account_pipeline(self, merge_request: MergeRequest) -> None:
        """Create the MR's CI pipeline as the service account (which can read private cross-project
        CI includes), used after a ``-o ci.skip`` push suppressed the ephemeral bot's pipeline.

        Best-effort and never raises: this runs at turn-end publish, so a failure here must not sink
        the whole publish. Because the push was skip-ci'd, a failed trigger leaves the MR with no
        pipeline — surface that loudly (error log for Sentry) and visibly (an MR note), never
        silently. A single attempt only: ``mr.pipelines.create()`` is a non-idempotent POST and
        python-gitlab may already retry transient errors, so we add no retry multiplier of our own.
        """
        try:
            pipeline = await sync_to_async(self.client.trigger_merge_request_pipeline)(
                merge_request.repo_id, merge_request.merge_request_id
            )
            logger.info(
                "Triggered CI pipeline %s for MR !%s as the service account",
                pipeline.id,
                merge_request.merge_request_id,
            )
        except Exception:
            logger.exception(
                "Could not trigger the CI pipeline for MR !%s as the service account; posting a note",
                merge_request.merge_request_id,
            )
            try:
                await sync_to_async(self.client.create_merge_request_comment)(
                    merge_request.repo_id,
                    merge_request.merge_request_id,
                    "⚠️ DAIV could not start the CI pipeline automatically. Please run it manually.",
                )
            except Exception:
                logger.exception("Could not post the pipeline-failure note on MR !%s", merge_request.merge_request_id)

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
        if self.ctx.issue:
            assignee = self.ctx.issue.assignee or self.ctx.issue.author
            assignee_id = assignee.id if self.ctx.git_platform == GitPlatform.GITLAB else assignee.username

        target_branch = (
            fallback_from_mr.target_branch
            if fallback_from_mr is not None
            else cast("str", self.ctx.config.default_branch)
        )

        return await sync_to_async(self.client.update_or_create_merge_request)(
            repo_id=self.ctx.repository.slug,
            source_branch=branch_name,
            target_branch=target_branch,
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
                    "session_url": await self._session_link("session_merge_request"),
                },
            ),
        )

    async def _session_link(self, route: str) -> str | None:
        """Absolute URL of ``route`` for the producing session, or None when unavailable.

        The link is cosmetic, so an unroutable thread_id or an unconfigured Sites row degrades
        to no link rather than losing the commit or the MR.
        """
        if not self.thread_id or not self.ctx.config.session_link or not site_settings.session_link_enabled:
            return None

        try:
            return await sync_to_async(build_absolute_url)(reverse(route, kwargs={"thread_id": self.thread_id}))
        except NoReverseMatch, ObjectDoesNotExist:
            logger.warning("Could not build a session link for thread_id %r; publishing without it", self.thread_id)
            return None

    async def _with_session_trailer(self, commit_message: str) -> str:
        """Append the session trailer to ``commit_message``, as its own trailing paragraph.

        Unlike the description link this survives description rewrites and stays attached to the
        commit once it is squashed or cherry-picked elsewhere.
        """
        session_url = await self._session_link("session_detail")
        if not session_url:
            return commit_message
        return f"{commit_message.rstrip()}\n\n{SESSION_TRAILER}: {session_url}"

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
