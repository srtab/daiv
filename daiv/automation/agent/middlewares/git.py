from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, NotRequired, cast

import httpx
import requests
from asgiref.sync import sync_to_async
from git import GitCommandError
from github import GithubException
from gitlab.exceptions import GitlabError
from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.prompts import SystemMessagePromptTemplate
from langsmith import get_current_run_tree

from automation.agent.git_manager import SandboxGitProtocolError
from automation.agent.git_utils import open_git_manager
from automation.agent.publishers import GitChangePublisher
from codebase.base import MergeRequest, Scope
from codebase.clients import RepoClient
from codebase.context import RuntimeCtx  # noqa: TC001
from codebase.utils import get_repo_ref

# Platform / transport errors that warrant a soft "no MR" fallback. The platform
# SDKs (python-gitlab, PyGithub) are requests-based, so raw network failures
# surface as requests exceptions, not Gitlab/Github API errors; httpx covers
# httpx-transported callees (e.g. repo-config fetches). Bugs (KeyError,
# AttributeError, etc.) propagate so the run fails loudly rather than producing
# a duplicate MR downstream.
_MR_LOOKUP_PLATFORM_ERRORS: tuple[type[BaseException], ...] = (
    GitlabError,
    GithubException,
    httpx.HTTPError,
    requests.RequestException,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.runtime import Runtime

    from automation.agent.middlewares.file_system import SandboxFileBackend


logger = logging.getLogger("daiv.tools")


GIT_SYSTEM_PROMPT = SystemMessagePromptTemplate.from_template(
    """\
## Git context

- Git platform: {{git_platform}}
- Repository ID: {{repository}}
- Current branch: {{current_branch}}
- Default branch: {{default_branch}}
- Git status: nothing to commit, working tree clean (This is the git status at the start of the conversation. Note that this status is a snapshot in time, and will not update during the conversation.)

**Committing and pushing is automatic.** The harness commits and pushes any file changes you make when your turn ends. You do not need to — and must not try to — run `git add`, `git commit`, `git push`, `git reset`, `git rebase`, `git config`, or any other index- or history-mutating git command. These are hard-blocked by sandbox policy; attempting them or their synonyms (`git stage`, `git update-index`, `git read-tree -m`, `git commit-tree`, …) will fail and waste turns.

- If a task tells you to "commit and push," interpret it as "make the edits" — the harness ships them.
- If a task asks you to "rebase" or "resolve merge conflicts with the target branch," tell the user this harness does not support rebase-style workflows and stop. Do not try to emulate rebase with `git show`/`git checkout -- <paths>`; it cannot complete without a staging primitive.
- Read-only git commands (`git log`, `git diff`, `git status`, `git show`, `git branch`, `git ls-files`, …) are allowed and useful for understanding branch state.
{{#issue_iid}}

You're currently working on issue #{{issue_iid}}.

The user will interact with you through the issue comments that will be automatically provided to you as messages. You should respond to the user's comments with the appropriate actions and tools.
{{/issue_iid}}

{{#merge_request_iid}}
You're currently working on merge request #{{merge_request_iid}}.

The user will interact with you through the merge request comments that will be automatically provided to you as messages. You should respond to the user's comments with the appropriate actions and tools.
{{/merge_request_iid}}""",  # noqa: E501
    "mustache",
)


class GitState(AgentState):
    """
    State for the git middleware.
    """

    merge_request: MergeRequest | None
    """
    The merge request used to commit the changes. Public on the output schema so
    it streams in AG-UI ``STATE_SNAPSHOT`` events — the chat UI's MR pill is wired
    directly to this field instead of a custom post-run event.
    """

    code_changes: Annotated[bool, PrivateStateAttr]
    """
    Whether the agent produced code changes that were published to the repository.
    """

    protected_branch_fallback_source: Annotated[str | None, PrivateStateAttr]
    """
    Source branch of the original MR when the publisher fell back to a fresh MR
    because that branch is protected on the remote. Consumed by managers so the
    notice can be appended to the agent's reply on the original MR rather than
    posted as a separate comment.
    """

    model_patch: NotRequired[str]
    """
    Unified diff of the run's working-tree changes vs ``HEAD`` (untracked files included),
    captured at turn end when the middleware runs with ``capture_patch=True`` (eval
    harnesses). Public on the output schema — like ``merge_request`` — so callers read it
    straight from ``ainvoke``'s returned state. Absent on normal runs.
    """

    pre_run_dirty_files: NotRequired[list[str]]
    """
    Files already differing from ``HEAD`` *before* the agent acted, detected at run start
    on ``capture_patch=True`` runs. Pre-existing dirt ends up inside ``model_patch`` (the
    capture is a plain working-tree diff), so eval harnesses need this machine-readable
    signal to flag/exclude the run instead of grading a poisoned patch. Public on the
    output schema like ``model_patch``; absent on normal runs and on clean starts.
    """


class GitMiddleware(AgentMiddleware[GitState, RuntimeCtx]):
    """
    Middleware to handle the git operations and persist changes made by the DAIV agent to the repository.

    When the agent's turn ends, the middleware commits and pushes the changes via
    :class:`GitChangePublisher` and creates a merge request if necessary. The resolved merge
    request is stored in state so subsequent turns reuse the same branch and merge request.

    Args:
        skip_ci: Whether to prefix the commit with ``[skip ci]``.
        auto_commit_changes: Whether the run publishes its changes at all. When ``False`` the
            turn-end publish is skipped.
        capture_patch: Whether to capture the run's working-tree diff (vs ``HEAD``) at turn end
            and expose it as ``model_patch`` in the output state. Used by eval harnesses to read
            the patch from ``ainvoke``'s result; keep ``False`` for normal runs so potentially
            large patches never stream through ``STATE_SNAPSHOT`` events.
        sandbox_backend: Run's bound :class:`SandboxFileBackend` injected by ``create_daiv_agent``;
            forwarded to :class:`GitChangePublisher` (and used for patch capture) so turn-end git
            runs inside the sandbox. ``None`` for sandbox-disabled / local runs.

    Example:
        ```python
        from langchain.agents import create_agent
        from langgraph.store.memory import InMemoryStore
        from automation.agent.middlewares.git import GitMiddleware

        store = InMemoryStore()

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[GitMiddleware()],
            store=store,
        )
        ```
    """

    state_schema = GitState

    def __init__(
        self,
        *,
        skip_ci: bool = False,
        auto_commit_changes: bool = True,
        capture_patch: bool = False,
        sandbox_backend: SandboxFileBackend | None = None,
    ) -> None:
        """
        Initialize the middleware.
        """
        self.skip_ci = skip_ci
        self.auto_commit_changes = auto_commit_changes
        self.capture_patch = capture_patch
        self._sandbox_backend = sandbox_backend

    async def abefore_agent(self, state: GitState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        Before the agent starts, resolve the merge request the run will publish to.

        No branch checkout happens here: ``set_runtime_ctx`` clones directly on the right
        ref (``ref=source_branch`` for MR scope; chat persists the branch), and the sandbox
        is seeded from that clone — so the workspace already reflects the MR's source branch
        before this hook runs. A post-seed checkout would only touch the now non-authoritative
        local clone, so the resolved MR is just recorded in state for the prompt and safeguard.

        On capture-patch runs the workspace is also checked for pre-existing changes: the
        captured ``model_patch`` is a working-tree diff vs ``HEAD`` (untracked files included)
        at turn end, so any dirt present *before* the agent acts (e.g. seeding artifacts)
        pollutes every patch and can break eval grading. Detection is loud-but-non-fatal —
        the run proceeds, with the dirty file list exposed as ``pre_run_dirty_files`` in
        state and on the LangSmith run tree so harnesses can flag the run programmatically.
        """
        pre_run_dirty_files: list[str] = []
        if self.capture_patch:
            pre_run_dirty_files = await self._acheck_pre_run_dirty(runtime)

        merge_request = state.get("merge_request")

        if runtime.context.scope == Scope.MERGE_REQUEST:
            # In this case, ignore the branch name and merge request ID from the state,
            # and use the source branch and merge request ID from the merge request.
            merge_request = runtime.context.merge_request
        elif merge_request is None:
            # Surface any pre-existing open MR on the current branch so the chat
            # composer pill reflects reality from the very first turn. Issue-scope
            # runs always start on the default branch, where this lookup short-circuits.
            merge_request = await self._alookup_open_mr(runtime.context)

        update: dict[str, Any] = {
            "merge_request": merge_request,
            "code_changes": False,
            "protected_branch_fallback_source": None,
        }
        if pre_run_dirty_files:
            update["pre_run_dirty_files"] = pre_run_dirty_files
        return update

    async def _acheck_pre_run_dirty(self, runtime: Runtime[RuntimeCtx]) -> list[str]:
        """
        Detect and loudly report files already differing from ``HEAD`` before the agent acts.

        Pre-existing dirt ends up verbatim inside the captured ``model_patch``, making empty
        runs look like wrong fixes and able to delete fixtures in an eval grading container —
        so besides the ERROR log, the file list is tagged on the LangSmith run tree and
        returned for the state update (see ``GitState.pre_run_dirty_files``). Non-fatal by
        design: the check is diagnostic, and a check failure must not abort an otherwise
        healthy run. The catch is deliberately narrow — wiring bugs (mode-mismatch
        ``RuntimeError``, asyncio misuse) must propagate, not degrade into a skipped check.
        """
        try:
            async with open_git_manager(
                sandbox_backend=self._sandbox_backend, gitrepo=runtime.context.gitrepo
            ) as git_manager:
                dirty_files = await git_manager.get_changed_files()
        except GitCommandError, httpx.HTTPError, SandboxGitProtocolError:
            logger.exception("Pre-run dirty-tree check failed; cannot verify the workspace is clean")
            return []

        if dirty_files:
            logger.error(
                "Workspace is dirty before the agent run; the captured model_patch will include "
                "pre-existing changes to %d file(s): %s",
                len(dirty_files),
                ", ".join(dirty_files[:20]) + (", …" if len(dirty_files) > 20 else ""),
            )
            if rt := get_current_run_tree():
                rt.metadata["pre_run_dirty_files"] = dirty_files
        return dirty_files

    @staticmethod
    async def _alookup_open_mr(context: RuntimeCtx) -> MergeRequest | None:
        """Best-effort lookup of an open MR whose source branch matches the current ref.

        Short-circuits on detached HEAD: commit-pinned runs (SWE-bench evals check out a
        raw SHA) have no branch, so no MR can exist and querying the platform with a SHA
        would be meaningless. Queries the run's platform — not the settings default, which
        for a repo living elsewhere would 404 or, worse, match a same-named repo's MR.
        Soft-fails on platform/transport errors so the agent can still run — the publisher
        will create a fresh MR if needed. Programming bugs propagate.
        """
        if context.gitrepo.head.is_detached:
            logger.debug("Skipping MR lookup for %s: detached HEAD (commit-pinned run)", context.repository.slug)
            return None
        current_branch = get_repo_ref(context.gitrepo)
        if not current_branch or current_branch == context.config.default_branch:
            return None
        try:
            client = RepoClient.create_instance(git_platform=context.git_platform)
            return await sync_to_async(client.get_merge_request_by_branches)(
                context.repository.slug, current_branch, context.config.default_branch
            )
        except _MR_LOOKUP_PLATFORM_ERRORS:
            logger.exception(
                "Failed to look up open merge request for %s on %s", context.repository.slug, current_branch
            )
            return None

    @staticmethod
    def _effective_mr_iid(
        *, context_mr: MergeRequest | None, state_mr: MergeRequest | None, current_ref: str
    ) -> int | None:
        """Pick the MR iid to advertise in the prompt.

        Prefer the context MR (MR-scope runs). Otherwise use the state MR only when its
        ``source_branch`` still matches the current ref — the branch could have been checked out from
        under us mid-run (or the MR closed), and a stale iid would mislead the agent every turn.
        """
        if context_mr is not None:
            return context_mr.merge_request_id
        if state_mr is not None and state_mr.source_branch == current_ref:
            return state_mr.merge_request_id
        return None

    async def awrap_model_call(
        self, request: ModelRequest[RuntimeCtx], handler: Callable[[ModelRequest[RuntimeCtx]], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the git system prompt.
        """
        # Prefer the MR resolved by ``abefore_agent`` (in state) — for chat triggers
        # ``context.merge_request`` is unset even when the current branch has an open
        # MR. Without this fallback the agent re-discovers via
        # ``project-merge-request list --source-branch ...`` on every turn and may
        # pick a different MR than the publisher will write to.
        current_ref = get_repo_ref(request.runtime.context.gitrepo)
        context_mr = request.runtime.context.merge_request
        state_mr = request.state.get("merge_request")
        mr_iid = self._effective_mr_iid(context_mr=context_mr, state_mr=state_mr, current_ref=current_ref)
        if mr_iid is None and context_mr is None and state_mr is not None:
            logger.warning(
                "[%s] Ignoring stale state MR #%s: source_branch=%r != current ref=%r",
                self.name,
                state_mr.merge_request_id,
                state_mr.source_branch,
                current_ref,
            )

        context = {
            "git_platform": request.runtime.context.git_platform.value,
            "repository": request.runtime.context.repository.slug,
            "current_branch": get_repo_ref(request.runtime.context.gitrepo),
            "default_branch": request.runtime.context.config.default_branch,
            "issue_iid": request.runtime.context.issue.iid if request.runtime.context.issue else None,
            "merge_request_iid": mr_iid,
        }

        system_prompt = ""
        if request.system_prompt:
            system_prompt = request.system_prompt + "\n\n"

        system_prompt += cast("str", GIT_SYSTEM_PROMPT.format(**context).content)

        return await handler(request.override(system_prompt=system_prompt))

    @staticmethod
    def _record_issue_mr(merge_request: MergeRequest, runtime: Runtime[RuntimeCtx]) -> None:
        """Tag the LangSmith run with the MR id when an issue produced a merge request."""
        if runtime.context.scope == Scope.ISSUE and (rt := get_current_run_tree()):
            rt.metadata["merge_request_id"] = merge_request.merge_request_id

    async def aafter_agent(self, state: GitState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        After the agent finishes, optionally capture the run's patch, then publish any new changes.

        Capture runs first: the publisher's commit moves ``HEAD``, which would empty a
        diff-vs-``HEAD`` taken afterwards. A capture failure raises when the run doesn't
        publish (the patch is the run's only artifact) but is logged-and-skipped when it
        does (capture is observability; the publish must not be aborted by it). The publish
        *decision* lives in :meth:`GitChangePublisher.publish`, which returns a
        :class:`PublishOutcome`: a no-op turn (clean tree already on its MR, or no changes at
        all) publishes nothing. We map the outcome onto the streamed ``merge_request`` field
        and the private ``code_changes`` / ``protected_branch_fallback_source`` flags.

        Short-circuited runs (a builtin slash command jumps from ``SlashCommandMiddleware.abefore_agent``
        straight to the after_agent chain) skip ``SandboxMiddleware.abefore_agent``, so the run's
        sandbox backend is never bound and the agent loop never ran — nothing was captured or changed.
        Probing git through the unbound backend would raise (``SandboxFileBackend is not bound to a
        sandbox session``), so detect it and no-op. Disk-backed runs pass ``sandbox_backend=None`` and
        keep a usable local clone, so they fall through and correctly report a clean tree.
        """
        if self._sandbox_backend is not None and not self._sandbox_backend.is_bound():
            logger.debug(
                "Sandbox backend not bound at turn end (run short-circuited before the agent loop); "
                "skipping patch capture and publish"
            )
            return None

        update: dict[str, Any] = {}
        if self.capture_patch:
            try:
                async with open_git_manager(
                    sandbox_backend=self._sandbox_backend, gitrepo=runtime.context.gitrepo
                ) as git_manager:
                    update["model_patch"] = await git_manager.get_diff()
            except GitCommandError, httpx.HTTPError, SandboxGitProtocolError:
                # Narrow on purpose: sandbox wire anomalies degrade, but wiring bugs (bare
                # RuntimeError from mode-mismatch guards, asyncio misuse) always propagate.
                # Not publishing (eval harnesses): the patch IS the run's artifact — fail loudly
                # rather than record an empty patch indistinguishable from "agent made no changes".
                if not self.auto_commit_changes:
                    raise
                # Publishing: capture is read-only observability — never strand the agent's work
                # uncommitted over it.
                logger.exception("Patch capture failed; publishing without model_patch")

        if not self.auto_commit_changes:
            return update or None

        publisher = GitChangePublisher(runtime.context, sandbox_backend=self._sandbox_backend)
        outcome = await publisher.publish(merge_request=state.get("merge_request"), skip_ci=self.skip_ci)

        if outcome.merge_request is None:
            return update or None

        self._record_issue_mr(outcome.merge_request, runtime)
        update |= {"merge_request": outcome.merge_request, "code_changes": True}
        if outcome.published:
            update["protected_branch_fallback_source"] = outcome.protected_branch_fallback_source
        return update
