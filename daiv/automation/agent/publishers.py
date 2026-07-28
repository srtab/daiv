from __future__ import annotations

import logging
from abc import abstractmethod
from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, urlencode

from django.template.loader import render_to_string

from asgiref.sync import sync_to_async
from git import GitCommandError

from automation.agent.git_utils import open_git_manager
from automation.agent.utils import build_langsmith_config
from codebase.base import GitPlatform, MergeRequest, Scope
from codebase.clients import RepoClient
from codebase.exceptions import MergeRequestBranchNotVisibleError
from codebase.utils import redact_diff_content
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_NAME
from core.site_settings import site_settings

from .diff_to_metadata.graph import create_diff_to_metadata_graph

if TYPE_CHECKING:
    from automation.agent.git_manager import GitManager
    from automation.agent.middlewares.file_system import SandboxFileBackend
    from codebase.clients.base import GitAuthEnv
    from codebase.context import RuntimeCtx


logger = logging.getLogger("daiv.tools")


@dataclass(frozen=True)
class PublishOutcome:
    """Result of a publish attempt.

    ``published`` is True when this turn committed/pushed/created/updated; False when there was
    nothing new (no changes at all, or a clean tree already on its MR). ``merge_request`` is the MR
    to surface in state (``None`` only when there was nothing at all, or when ``pending_branch`` is
    set). ``protected_branch_fallback_source`` is the original MR's source branch when publish fell
    back to a fresh MR because that branch was protected on the remote (``None`` otherwise);
    consumed by managers to bundle a notice into the reply. ``pending_branch`` is the branch this
    turn pushed when the platform would not yet open an MR for it — the work is on the remote, so the
    run reports it instead of failing, and a later turn retries the MR on that same branch rather
    than minting a duplicate one. ``pending_branch_verified`` says whether that branch was actually
    confirmed on the remote (see :class:`MergeRequestBranchNotVisibleError`); an unconfirmed branch is
    still worth reporting but must not be described to the user as safe.

    The invariants below are checked rather than merely documented: every consumer branches on this
    combination to decide what the user is told and what gets checkpointed, and there are two
    independent call sites, so an illegal combination silently reports the wrong thing.
    """

    merge_request: MergeRequest | None
    published: bool
    protected_branch_fallback_source: str | None = None
    pending_branch: str | None = None
    pending_branch_verified: bool = False

    def __post_init__(self) -> None:
        if self.pending_branch and self.merge_request is not None:
            raise ValueError("pending_branch and merge_request are mutually exclusive: the MR is already open.")
        if self.pending_branch and not self.published:
            raise ValueError("A pending_branch means the work was pushed, so published must be True.")
        if self.published and self.merge_request is None and not self.pending_branch:
            raise ValueError("A published turn must report where the work went: a merge_request or a pending_branch.")
        if self.pending_branch_verified and not self.pending_branch:
            raise ValueError("pending_branch_verified describes a pending_branch, so it needs one.")

    @classmethod
    def pending(
        cls,
        branch: str,
        error: MergeRequestBranchNotVisibleError,
        *,
        protected_branch_fallback_source: str | None = None,
    ) -> PublishOutcome:
        """The outcome for a turn whose work was pushed but whose MR could not be opened.

        Both create sites degrade the same way, so the field combination lives here — the previous two
        hand-built copies had already drifted on which fields they carried.
        """
        return cls(
            merge_request=None,
            published=True,
            protected_branch_fallback_source=protected_branch_fallback_source,
            pending_branch=branch,
            pending_branch_verified=error.verified,
        )

    def state_update(self, *, had_pending_branch: bool) -> dict[str, Any]:
        """The ``GitState`` keys this outcome implies, for whoever is checkpointing it.

        Two independent callers write this state — ``GitMiddleware.aafter_agent`` at the end of a normal
        turn and ``BaseManager._recover_draft`` after an agent error — and they must agree, because the
        keys interlock: an MR settles a pending debt, a pending branch invalidates a state MR, and a
        turn that published nothing has to expire a debt publish says is gone while leaving an
        outstanding one alone. They drifted once already, so the mapping lives here rather than being
        written twice.

        ``had_pending_branch`` is whether state already owed a branch; without it a nothing-published
        outcome cannot tell "expire the stale debt" from "leave state untouched".
        """
        if self.merge_request is not None:
            update: dict[str, Any] = {
                "merge_request": self.merge_request,
                "code_changes": True,
                "pending_mr_branch": None,
                "pending_mr_branch_verified": False,
            }
            if self.published and (self.protected_branch_fallback_source or not had_pending_branch):
                # A no-op turn must not clobber a prior turn's fallback signal — and neither must the
                # turn that finally settles an outstanding debt: the fallback happened on the turn that
                # created the owed branch, so overwriting it with this turn's `None` would drop the only
                # explanation for why the reviewer is being sent to a different merge request.
                update["protected_branch_fallback_source"] = self.protected_branch_fallback_source
            return update

        if self.pending_branch:
            return {
                # publish only reaches MR-create when it has no MR to update — either none was in state,
                # or the state MR was abandoned because its source branch is protected. So if one *was*
                # in state it no longer holds this turn's work: leaving it would link the wrong MR and
                # suppress the pending notice.
                "merge_request": None,
                "pending_mr_branch": self.pending_branch,
                "pending_mr_branch_verified": self.pending_branch_verified,
                "code_changes": True,
                "protected_branch_fallback_source": self.protected_branch_fallback_source,
            }

        if had_pending_branch:
            # Publish looked at the owed branch and found nothing outstanding (merged, or gone from the
            # remote). Expiring it here is what stops the reply's notice repeating forever.
            return {"pending_mr_branch": None, "pending_mr_branch_verified": False}

        return {}


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
        self,
        *,
        merge_request: MergeRequest | None = None,
        skip_ci: bool = False,
        as_draft: bool = False,
        pending_branch: str | None = None,
        **kwargs,
    ) -> PublishOutcome:
        """
        Daiv-direct publish: ensure the run's changes reach a merge request.

        Computes one ``status_snapshot`` and decides whether anything is new (folding the former
        ``GitMiddleware._is_unpublished`` gate): a clean tree whose work is already on its MR — or no
        changes at all — short-circuits without an LLM metadata call or a no-op push. Otherwise
        commits any uncommitted work (LLM-generated message), pushes, and opens/updates the MR.

        ``pending_branch`` is a branch an earlier turn pushed but could not open an MR for (see
        :attr:`PublishOutcome.pending_branch`). Reusing it is what stops the retry from piling up
        near-identical branches and MRs: without it, every later turn generates a fresh unique
        branch name and the abandoned ones accumulate on the remote.
        """
        protected_branch_fallback_source: str | None = None
        default_branch = cast("str", self.ctx.config.default_branch)

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
            # Diff against what the run's work is actually reviewed against — the MR's target branch
            # when the run is inside one, else the repo default. Always diffing the default branch
            # sweeps in every commit separating it from the real base, so a one-file change on a branch
            # stacked off a release branch reached diff_to_metadata as that branch's entire delta
            # against the default: the commit message and PR description then described the wrong work
            # (and the metadata call paid for the whole diff). Note a run on a non-default ref with no
            # MR at all still diffs the default branch and, on publish, targets it — there is no other
            # base to infer for a branch nobody is reviewing yet.
            requested_base = merge_request.target_branch if merge_request is not None else default_branch
            snapshot = await git_manager.status_snapshot(
                base_branch=requested_base,
                mr_source_branch=merge_request.source_branch if merge_request is not None else None,
                fallback_base_branch=default_branch,
            )
            if snapshot.diff_base != requested_base:
                # The requested base wasn't in the clone, so everything generated below (commit message,
                # PR description, branch name) describes the diff against a base nobody asked for — the
                # exact failure mode this base-branch choice exists to avoid. Say so rather than leaving
                # it as a line in the git layer's log.
                logger.warning(
                    "Base branch 'origin/%s' is not in this clone, so the change metadata was generated "
                    "against 'origin/%s' instead; it may describe unrelated history.",
                    requested_base,
                    snapshot.diff_base,
                )

            if not snapshot.dirty:
                if not snapshot.diff.strip():
                    if merge_request is None and pending_branch:
                        # This turn added nothing, but an earlier one still owes an MR. Short-circuiting
                        # here is what made the advertised remedy ("re-run and DAIV opens it on that
                        # branch") a promise the code never kept: an issue-scope re-run starts from a
                        # fresh clone of the default branch, so it lands here every time and the branch
                        # would stay MR-less forever.
                        return await self._open_owed_merge_request(
                            git_manager, pending_branch, default_branch, as_draft=as_draft
                        )
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

            if merge_request is not None:
                branch_name = merge_request.source_branch
            elif pending_branch:
                branch_name = pending_branch
            else:
                branch_name = git_manager.unique_branch_name(
                    changes_metadata["pr_metadata"].branch, snapshot.remote_branches
                )

            # Only a branch that already exists on the remote may have advanced under the run (a
            # dependabot force-push, a concurrent push, or — for a pending branch — this session's
            # own earlier turn, which a re-cloned workspace is no longer a descendant of) —
            # integrate + retry there so the work isn't lost. A freshly minted, unique branch can't,
            # so leave integration off for those.
            reusing_remote_branch = merge_request is not None or bool(pending_branch)
            await git_manager.push_head_to(branch_name, integrate_on_reject=reusing_remote_branch)

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
            except MergeRequestBranchNotVisibleError as e:
                # The commits are pushed and only the MR is missing. Failing here would discard a
                # completed turn (and orphan the branch), so report the branch instead and let a later
                # turn open the MR on it. Error level, not warning: this is the operator's only signal
                # that a branch exists without an MR, and only ERROR reaches Sentry.
                logger.exception(
                    "Published changes to branch '%s' but the merge request could not be opened yet; "
                    "the platform still reports the branch as missing (branch confirmed on remote: %s).",
                    branch_name,
                    e.verified,
                )
                return PublishOutcome.pending(
                    branch_name, e, protected_branch_fallback_source=protected_branch_fallback_source
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

    async def _open_owed_merge_request(
        self, git_manager: GitManager, pending_branch: str, default_branch: str, *, as_draft: bool
    ) -> PublishOutcome:
        """Open the merge request an earlier turn owes for ``pending_branch``, changing nothing else.

        Reached only when this turn produced no changes of its own, so there is nothing to commit or
        push — the work is already on the remote branch and all that is missing is the MR. The metadata
        therefore has to describe the *branch* (``get_range_diff``) rather than the working tree, which
        in a re-cloned workspace no longer contains those commits at all.

        Known limitation: the MR targets the repo default and carries no back-link. If the pending branch
        only exists because an earlier turn's protected source branch forced a replacement MR, that
        original MR is not carried across turns, so the retry can neither inherit its target branch nor
        reference it the way an in-turn fallback does. The protected-branch *footer* still renders, since
        that signal is kept in state while the debt is outstanding.
        """
        try:
            diff = await git_manager.get_range_diff(base_branch=default_branch, head_branch=pending_branch)
        except GitCommandError:
            # The branch is not in the clone — merged and auto-deleted after the user opened the MR by
            # hand (which this run's own notice suggests), deleted by an operator cleaning up the orphan,
            # or never created at all in the unverified case. Letting this propagate would be far worse
            # than losing the debt: it escapes publish, takes the agent's reply with it, and — because it
            # raises before any state write — leaves the branch checkpointed so every later turn on the
            # thread fails the same way. Void the debt instead, loudly.
            logger.exception(
                "Owed merge request for branch '%s' cannot be opened: 'origin/%s' does not resolve in this "
                "clone. Dropping the pending branch; if it does exist on the remote, its merge request must "
                "be opened by hand.",
                pending_branch,
                pending_branch,
            )
            return PublishOutcome(merge_request=None, published=False)

        if not diff.strip():
            # Nothing on the branch either: it holds no work, so there is nothing to open an MR for and
            # nothing to keep owing. Reporting "no changes" lets the caller drop the stale pending state.
            # Error level because this is the one place DAIV forgets state it told the user holds their
            # changes — the usual cause is the branch having been merged, but an operator needs to be
            # able to tell that from the alternatives.
            logger.error(
                "Owed merge request for branch '%s' has no changes against '%s' (already merged, or an "
                "ancestor of it); dropping the pending branch.",
                pending_branch,
                default_branch,
            )
            return PublishOutcome(merge_request=None, published=False)

        # No commit message: this path commits nothing, so asking for one would buy a discarded
        # LLM call over the whole branch diff.
        changes_metadata = await self._diff_to_metadata(pr_metadata_diff=diff)
        try:
            merge_request = await self._create_merge_request(
                pending_branch,
                changes_metadata["pr_metadata"].title,
                changes_metadata["pr_metadata"].description,
                as_draft=as_draft,
            )
        except MergeRequestBranchNotVisibleError as e:
            logger.exception(
                "Still could not open the owed merge request for branch '%s' (branch confirmed on remote: %s).",
                pending_branch,
                e.verified,
            )
            return PublishOutcome.pending(pending_branch, e)

        logger.info("Opened the owed merge request for branch '%s': %s", pending_branch, merge_request.web_url)
        await self._suggest_context_file(merge_request)
        return PublishOutcome(merge_request=merge_request, published=True)

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

    async def _diff_to_metadata(
        self, commit_message_diff: str | None = None, pr_metadata_diff: str | None = None
    ) -> dict[str, Any]:
        """
        Get the PR metadata from the diff.

        The two halves run as independent agents, so each diff is only sent to the model when its half
        was asked for. Pass ``commit_message_diff=None`` when there is nothing to commit — the owed-MR
        retry, for instance — or the run pays for a whole commit-message call whose result is discarded.

        Args:
            ctx: The runtime context.
            commit_message_diff: The diff of the commit message. If None, the commit message will not be
                computed.
            pr_metadata_diff: The diff of the PR metadata. If None, the PR metadata will not be computed.

        Returns:
            The pull request metadata and commit message.
        """

        input_data: dict[str, str] = {}
        if commit_message_diff:
            input_data["commit_message_diff"] = redact_diff_content(
                commit_message_diff, self.ctx.config.omit_content_patterns
            )
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

        changes_metadata_graph = create_diff_to_metadata_graph(
            ctx=self.ctx, include_pr_metadata=bool(pr_metadata_diff), include_commit_message=bool(commit_message_diff)
        )
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
                can trace the relationship, and its target branch is inherited.

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

        # A genuinely new MR targets the repo default. One that only exists because the original's
        # source branch was protected inherits that original's target instead — the work was under
        # review against, say, a release branch, and silently re-pointing it at the default branch
        # would make the replacement MR a different (much larger) change than the one it replaces.
        target_branch = (
            fallback_from_mr.target_branch if fallback_from_mr else cast("str", self.ctx.config.default_branch)
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
