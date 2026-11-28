from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, NotRequired, cast

import httpx
from git import GitCommandError
from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.prompts import SystemMessagePromptTemplate
from langsmith import get_current_run_tree

from automation.agent.context import RepoHandle, RuntimeCtx, SubmoduleRole
from automation.agent.events import (
    PUBLISH_RESULT_EVENT,
    publish_result_payload,
    published_outcome_payload,
    repo_publish_payload,
)
from automation.agent.git_state import RepoPublish, checkpointed_merge_request, effective_merge_request
from automation.agent.publish_errors import publish_error_kind
from automation.agent.publishers import (
    GitChangePublisher,
    PublishOutcome,
    UnresolvedMergeConflictsError,
    aopen_branch_merge_request,
    apublish_extra_repos,
    apublish_submodules,
    mark_recorded,
    publish_failure,
)
from automation.agent.sibling_links import RelatedMergeRequest, aensure_related_links
from automation.agent.synthetic import PUBLISH_FAILURES_KIND, synthetic_ai_message
from automation.agent.utils import final_assistant_text, streamed_assistant_message
from automation.agent.workspace.git_manager import KeptOut, KeptOutSubmodule, PublishScope
from automation.agent.workspace.git_runners import SandboxGitProtocolError
from codebase.base import GitPlatform, MergeRequest, Scope
from codebase.clients.checkout import CheckoutReport, PublishBlock, SkipReason, SubmoduleNote

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from langgraph.runtime import Runtime

    from automation.agent.agent_settings import AgentSettings
    from automation.agent.session_links import SessionLinksResolver
    from automation.agent.workspace.base import Workspace


logger = logging.getLogger("daiv.tools")


GIT_SYSTEM_PROMPT = SystemMessagePromptTemplate.from_template(
    """\
## Git context

- Git platform: {{git_platform}}
- Repository ID: {{repository}}
- Current branch: {{current_branch}}
- Default branch: {{default_branch}}
- Git status: nothing to commit, working tree clean (This is the git status at the start of the conversation. Note that this status is a snapshot in time, and will not update during the conversation.)

**Committing and pushing is automatic.** The harness commits and pushes any file changes you make when your turn ends.

- If a task tells you to "commit and push," interpret it as "make the edits" — the harness ships them.
- If a task asks you to merge a branch in (usually the target branch) or to resolve merge conflicts with it, merge it without committing and let the harness commit the merge:
    1. Run `git fetch origin <branch>`, then `git merge --no-commit --no-ff origin/<branch>`. Only that form and `git merge --abort` (to start over) are allowed: a merge without `--no-commit --no-ff`, or with `-m`, `--squash` or `--continue`, is blocked. If the fetch fails, merge the `origin/<branch>` you already have.
    2. List the conflicted files with `git diff --name-only --diff-filter=U`. Compare the sides with `git show :1:<path>` (common base), `git show :2:<path>` (this branch) and `git show :3:<path>` (the merged branch), and read why each side changed with `git log --merge -p -- <path>`. Some conflicts have no markers (a file deleted on one side, a binary file): decide each one explicitly.
    3. Edit each conflicted file to the result that keeps both sides' intent, or take one side whole with `git restore --ours <path>` or `git restore --theirs <path>`. Delete a file the merge should not keep. Do not try to stage the result.
    4. Before you finish, make sure no conflicted file still has a `<<<<<<<` or `>>>>>>>` line, then run the tests that cover the merged code. The harness does not publish a merge that still has conflict markers.
- If a task asks you to "rebase," tell the user this harness cannot rebase and offer to merge the target branch in instead.
- Read-only git commands (`git log`, `git diff`, `git status`, `git show`, `git branch`, `git ls-files`, …) are allowed and useful for understanding branch state.
{{#issue_iid}}

You're currently working on issue #{{issue_iid}}.

The user will interact with you through the issue comments that will be automatically provided to you as messages. You should respond to the user's comments with the appropriate actions and tools.
{{/issue_iid}}

{{#merge_request_iid}}
You're currently working on merge request #{{merge_request_iid}}.

The user will interact with you through the merge request comments that will be automatically provided to you as messages. You should respond to the user's comments with the appropriate actions and tools.
{{/merge_request_iid}}
{{#has_other_repositories}}

## Other repositories in this session

The repository above is the main one, checked out at `/workspace/repo`. This session also has:
{{#other_repositories}}
- `{{repository}}` at `{{path}}` (current branch: `{{current_branch}}`, default branch: `{{default_branch}}`)
{{/other_repositories}}

- Each is its own git repository: run git in it with `git -C <path> …`.
- Fix a problem in the repository whose code causes it; never work around one repository's bug in another.
- When your turn ends, each repository's changes are committed, pushed and opened as their own merge request.
- A repository's own agent instructions, if any, are in `<path>/.agents/AGENTS.md` or `<path>/AGENTS.md`: read them before changing that repository.
{{/has_other_repositories}}
{{#has_checkout_sections}}

{{&checkout_sections}}
{{/has_checkout_sections}}""",  # noqa: E501
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

    published: NotRequired[bool]
    """
    Whether *this turn* committed and pushed. Narrower than ``code_changes``, which stays true
    for a clean tree already on its merge request — so it cannot answer "did this turn push?",
    which is what the CI watch arms on. The run executor reads it from the checkpoint once the
    run finishes; chat's ``STATE_SNAPSHOT`` stream never carries it (``STREAMED_STATE_KEYS``).
    """

    diff_stats: NotRequired[dict[str, int]]
    """
    ``{"lines_added", "lines_removed", "files_changed"}`` for the work published so far.
    Public on the output schema like ``merge_request``, so the chat composer's ``+x −y``
    pill updates from the same ``STATE_SNAPSHOT`` that carries the MR. A plain dict rather
    than the pydantic model: three ints have nothing to gain from an ``lc:2`` envelope
    round-trip through the checkpointer.
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

    repo_publishes: Annotated[NotRequired[list[dict[str, Any]]], PrivateStateAttr]
    """
    This turn's publish of each of the session's extra repositories, as
    :meth:`~automation.agent.publishers.RepoPublish.to_state` dicts in the context's order.
    Set at turn end whenever the run has extra repositories; ``merge_request`` and
    ``diff_stats`` stay the main repository's.
    """

    main_publish_error: Annotated[NotRequired[str | None], PrivateStateAttr]
    """
    Why the main repository's publish failed in a turn with extra repositories, which ends the
    turn with a notice instead of raising so the extra repositories' publishes are kept. ``None``
    otherwise; a run without extra repositories still raises.
    """


def _turn_publish_reset() -> dict[str, Any]:
    """The turn's publish results before anything published; ``abefore_agent`` starts every agent-loop turn from it."""
    return {"code_changes": False, "published": False, "repo_publishes": [], "main_publish_error": None}


LIST_CAP = 20

SKIP_PHRASES = {
    SkipReason.OUTSIDE_PLATFORM: "it is outside this git platform",
    SkipReason.NOT_CATALOGUED: "DAIV doesn't work on that repository",
    SkipReason.DAIV_NO_ACCESS: "DAIV's account can't read that repository",
    SkipReason.PERSON_NO_ACCESS: "the person this run belongs to can't read that repository",
    SkipReason.COMMIT_MISSING: "the commit this branch records no longer exists in that repository",
    SkipReason.OVER_LIMIT: "over the LFS download limit",
    SkipReason.FETCH_FAILED: "the download failed",
}


def block_phrase(role: SubmoduleRole) -> str:
    """Why DAIV doesn't publish a submodule, as the agent and the person read it."""
    if role.publish_block is PublishBlock.PUBLISHED_ELSEWHERE:
        return f"that repository is published from `{role.published_from}`"
    if role.publish_block is PublishBlock.PARENT_NOT_PUBLISHED:
        return "the submodule that contains it is read-only"
    return "the person this run belongs to can't write to that repository"


def _capped[T](items: Sequence[T]) -> tuple[list[T], int]:
    return list(items[:LIST_CAP]), max(len(items) - LIST_CAP, 0)


def checkout_section(handle: RepoHandle) -> str:
    """The prompt's section on what completing ``handle``'s clone checked out and left out, and which submodules DAIV
    publishes (contracts/agent-context.md §1); ``""`` for a repository that didn't opt in."""
    report = handle.checkout
    if report.is_empty:
        return ""
    lines = [f"## Submodules and LFS files in `{handle.repository.slug}`", ""]
    published = [sub for sub in handle.submodules if cast("SubmoduleRole", sub.submodule).publish_block is None]
    read_only = [sub for sub in handle.submodules if cast("SubmoduleRole", sub.submodule).publish_block is not None]
    if published:
        lines.append(
            "- Submodules whose changes are published as their own merge request, with this repository updated to "
            "point at it:"
        )
        shown, more = _capped(published)
        for sub in shown:
            role = cast("SubmoduleRole", sub.submodule)
            entry = f"  - `{role.path}` (`{sub.repository.slug}`, into `{role.target_branch}`)"
            if role.note is SubmoduleNote.BRANCH_MOVED_ON:
                entry += (
                    ": its branch no longer contains the commit this repository records, so changes there open a new "
                    "merge request"
                )
            elif not sub.head_detached:
                entry += f": continues its open merge request on its branch `{sub.current_ref}`"
            lines.append(f"{entry}.")
        if more:
            lines.append(f"  - and {more} more.")
    if read_only:
        lines.append("- Submodules checked out read-only (changes there are not published):")
        shown, more = _capped(read_only)
        for sub in shown:
            role = cast("SubmoduleRole", sub.submodule)
            lines.append(f"  - `{role.path}` (`{sub.repository.slug}`): {block_phrase(role)}.")
        if more:
            lines.append(f"  - and {more} more.")
    if skipped := report.skipped_submodules:
        lines.append("- Not checked out (the directory is empty):")
        shown_skipped, more = _capped(skipped)
        for sub in shown_skipped:
            lines.append(
                f"  - `{sub.path}` (`{sub.repo_id or sub.url}`): {SKIP_PHRASES[cast('SkipReason', sub.skipped)]}."
            )
        if more:
            lines.append(f"  - and {more} more.")
    if report.options.lfs and (skipped_files := report.skipped_lfs_files):
        shown_files, more = _capped(skipped_files)
        names = ", ".join(f"`{file.path}` ({SKIP_PHRASES[cast('SkipReason', file.skipped)]})" for file in shown_files)
        lines.append(
            f"- LFS files not downloaded (they hold their pointer text): {names}{f', and {more} more' if more else ''}."
        )
    lines.append(_not_published_bullet(report))
    return "\n".join(lines)


def _not_published_bullet(report: CheckoutReport) -> str:
    parts = []
    if report.options.lfs:
        parts.append("Changes to LFS-tracked files")
    if report.options.submodules:
        parts += ["adding or removing a submodule", "changes inside read-only submodules"]
    what = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])}, and {parts[-1]}"
    what = what[0].upper() + what[1:]
    bullet = f"- {what} are not published: the harness leaves them out of the commit and says so when your turn ends."
    if report.options.submodules:
        bullet += " Don't commit inside a submodule; the harness does that for the published ones."
    return bullet


def _sibling_notices(ctx: RuntimeCtx, repo_publishes: list[RepoPublish]) -> tuple[list[str], str | None]:
    """The turn-end notices of the extra repositories: one naming the conflicted files of each that stopped on merge
    conflicts, and the list of the others whose publish failed (``None`` when none did). A submodule's failure is in
    its root's kept-out notice instead."""
    slugs = {handle.repo_id: handle.repository.slug for handle in ctx.extra_repos}
    conflicts: list[str] = []
    failed: list[str] = []
    for publish in repo_publishes:
        if publish.submodule_path or publish.repo_id not in slugs:
            continue
        slug = slugs[publish.repo_id]
        if publish.conflicted_paths:
            conflicts.append(_conflicts_notice(publish.conflicted_paths, where=f" in `{slug}`"))
        elif publish.error:
            failed.append(f"- `{slug}`: {publish.error}")
    failures = "\n".join(["I couldn't publish the changes in these repositories:", *failed]) if failed else None
    return conflicts, failures


def _merge_request_ref(handle: RepoHandle, merge_request: MergeRequest) -> str:
    separator = "!" if handle.client.git_platform == GitPlatform.GITLAB else "#"
    return f"{handle.repository.slug}{separator}{merge_request.merge_request_id}"


def kept_out_notice(
    root: RepoHandle, kept_out: KeptOut | None, submodule_publishes: Sequence[RepoPublish], *, where: str
) -> str | None:
    """The turn-end notice of what ``root``'s publish and its submodules' publishes left out, the submodules that
    couldn't be published, and the pins applied or left (contracts/agent-context.md §2); ``None`` when there is none.
    ``where`` is a trailing place phrase or ``""``."""
    subs = {cast("SubmoduleRole", sub.submodule).path: sub for sub in root.submodules}
    lines: list[str] = []
    sources: list[tuple[str, KeptOut | None]] = [("", kept_out)]
    sources += [(f"{publish.submodule_path}/", publish.kept_out) for publish in submodule_publishes]
    lfs_paths: list[str] = []
    for prefix, source in sources:
        if not source:
            continue
        lfs_paths += [f"{prefix}{path}" for path in source.lfs_paths]
        for entry in source.submodules:
            lines.append(_kept_out_line(f"{prefix}{entry.path}", entry, subs, submodule_publishes))
    for publish in submodule_publishes:
        if publish.error:
            lines.append(
                f"- The submodule `{publish.submodule_path}` couldn't be published, so its parent still points at its "
                "old commit."
            )
    if lfs_paths:
        shown, more = _capped(lfs_paths)
        lines.append(
            f"- LFS-tracked files: {', '.join(f'`{path}`' for path in shown)}{f', and {more} more' if more else ''}."
        )
    pins = _pin_lines(subs, submodule_publishes)
    if not lines and not pins:
        return None
    paragraphs = [f"I didn't publish some of my changes{where}:\n" + "\n".join(lines)] if lines else []
    return "\n\n".join([*paragraphs, *pins])


def _kept_out_line(
    path: str, entry: KeptOutSubmodule, subs: dict[str, RepoHandle], publishes: Sequence[RepoPublish]
) -> str:
    if entry.remote_moved:
        line = (
            f"- The submodule `{path}` is still at the commit the branch's latest push set: someone moved it while I "
            "worked, and their commit wins."
        )
        merge_request = next(
            (p.merge_request for p in publishes if p.submodule_path == path and p.merge_request is not None), None
        )
        if merge_request is not None and path in subs:
            line += f" My changes there are in `{_merge_request_ref(subs[path], merge_request)}`."
        return line
    if entry.removed:
        return f"- The submodule `{path}` was removed or moved, which DAIV doesn't publish."
    if entry.added:
        return f"- `{path}` was added as a submodule, which DAIV doesn't publish."
    if entry.changed_files:
        why = f" ({block_phrase(cast('SubmoduleRole', subs[path].submodule))})" if path in subs and entry.block else ""
        files = ", ".join(f"`{name}`" for name in entry.changed_files)
        more = f", and {entry.more} more" if entry.more else ""
        return f"- Inside the read-only submodule `{path}`{why}: {files}{more}."
    return f"- The submodule `{path}` points at another commit, which DAIV doesn't publish."


def _pin_lines(subs: dict[str, RepoHandle], publishes: Sequence[RepoPublish]) -> list[str]:
    recorded = {publish.submodule_path: publish.recorded_sha for publish in publishes}
    lines = []
    for path, sub in subs.items():
        role = cast("SubmoduleRole", sub.submodule)
        if role.pinned and recorded.get(path) == role.pinned:
            lines.append(
                f"Updated the submodule `{path}` to `{role.pinned[:7]}`, the commit its merge request in "
                f"`{sub.repository.slug}` was merged as."
            )
        elif role.note is SubmoduleNote.PIN_LEFT:
            lines.append(
                f"Left the submodule `{path}` as it is: this branch no longer records the commit DAIV pushed to "
                f"`{sub.repository.slug}`."
            )
    return lines


def _conflicts_notice(paths: Sequence[str], *, where: str) -> str:
    """The reply naming the files still holding conflict markers; ``where`` is a trailing place phrase or ``""``."""
    there = " there" if where else ""
    return (
        f"I didn't publish the merge{where}: {', '.join(f'`{path}`' for path in paths)} still "
        f"{'has' if len(paths) == 1 else 'have'} conflict markers, so nothing was committed or "
        f"pushed{there}. Ask me to finish resolving them."
    )


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
            the patch from the run's final state; keep ``False`` for normal runs so potentially
            large patches never stream through ``STATE_SNAPSHOT`` events.
        workspace: The run's workspace; its git backs the pre-run check and patch capture, and the publisher uses it.
        settings: The run's resolved agent settings.
        session_links: What builds the links the publisher puts in the commit and the merge request; ``None`` for none.
        fork_branch: Whether a turn with no merge request of its own leaves an open one on the checked-out branch
            alone and publishes to a new branch and merge request instead of pushing onto it.

    Example:
        ```python
        from langchain.agents import create_agent
        from langgraph.store.memory import InMemoryStore
        from automation.agent.middlewares.git import GitMiddleware

        store = InMemoryStore()

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[GitMiddleware(workspace=workspace, settings=settings)],
            store=store,
        )
        ```
    """

    state_schema = GitState

    def __init__(
        self,
        *,
        workspace: Workspace,
        settings: AgentSettings,
        skip_ci: bool = False,
        auto_commit_changes: bool = True,
        capture_patch: bool = False,
        session_links: SessionLinksResolver | None = None,
        fork_branch: bool = False,
    ) -> None:
        """
        Initialize the middleware.
        """
        self.skip_ci = skip_ci
        self.auto_commit_changes = auto_commit_changes
        self.capture_patch = capture_patch
        self.fork_branch = fork_branch
        self._workspace = workspace
        self._settings = settings
        self._session_links = session_links

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
            pre_run_dirty_files = await self._acheck_pre_run_dirty()

        merge_request = self._state_merge_request(state)

        if runtime.context.scope == Scope.MERGE_REQUEST:
            # In this case, ignore the branch name and merge request ID from the state,
            # and use the source branch and merge request ID from the merge request.
            merge_request = runtime.context.merge_request
        elif not self.fork_branch and (
            merge_request is None or merge_request.source_branch != runtime.context.repo.current_ref
        ):
            # Surface any pre-existing open MR on the current branch so the chat composer pill
            # reflects reality from the very first turn. A *first* issue-scope turn starts on the
            # default branch, where this lookup short-circuits; a follow-up turn starts on the
            # branch the previous turn published to (``Session.ref``, resolved in
            # ``address_issue_task``), so a cleared checkpoint re-discovers that MR here.
            merge_request = await self._alookup_open_mr(runtime.context) or merge_request

        update: dict[str, Any] = {
            "merge_request": merge_request,
            "protected_branch_fallback_source": None,
            **_turn_publish_reset(),
        }
        if pre_run_dirty_files:
            update["pre_run_dirty_files"] = pre_run_dirty_files
        return update

    async def _acheck_pre_run_dirty(self) -> list[str]:
        """
        Detect and loudly report files already differing from ``HEAD`` before the agent acts.

        Pre-existing dirt ends up verbatim inside the captured ``model_patch``, making empty
        runs look like wrong fixes and able to delete fixtures in an eval grading container —
        so besides the ERROR log, the file list is tagged on the LangSmith run tree and
        returned for the state update (see ``GitState.pre_run_dirty_files``). Non-fatal by
        design: the check is diagnostic, and a check failure must not abort an otherwise
        healthy run. The catch is deliberately narrow — wiring bugs (unbound-session
        ``RuntimeError``, asyncio misuse) must propagate, not degrade into a skipped check.
        """
        try:
            dirty_files = await self._workspace.git.get_changed_files()
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
        """Best-effort lookup of an open MR whose source branch is the main repository's current ref."""
        return await aopen_branch_merge_request(context.repo)

    def _effective_mr(self, state: GitState, context: RuntimeCtx, *, current_ref: str) -> MergeRequest | None:
        """Resolve the run's MR from the one decision, with the context this middleware holds.

        Both hops that need it — the prompt's iid and the publish target — go through here so they
        cannot drift on *which* refs they compare; splitting them is what lost a turn's work.
        """
        return effective_merge_request(
            context_mr=context.merge_request, state_mr=self._state_merge_request(state), current_ref=current_ref
        )

    def _publish_target(self, state: GitState, runtime: Runtime[RuntimeCtx]) -> MergeRequest | None:
        """The MR ``publish()`` may add onto, or ``None`` to open a fresh branch and MR.

        The log level splits on *who chose the ref*: on an issue run DAIV chose it itself
        (``Session.ref``), so a mismatch is its own continuity failing and ERROR is the only level
        this project turns into a Sentry event. Elsewhere the caller chose it — a chat turn
        retargeting the branch pill lands here legitimately, and paging on it trains the signal away.
        """
        current_ref = runtime.context.repo.current_ref
        publish_mr = self._effective_mr(state, runtime.context, current_ref=current_ref)
        if publish_mr is None and (state_mr := self._state_merge_request(state)) is not None:
            log = logger.error if runtime.context.scope == Scope.ISSUE else logger.warning
            log(
                "[%s] Not publishing to MR #%s: its source_branch=%r is not the checked-out ref=%r. "
                "Opening a fresh branch and MR instead so this run's work isn't rebased onto unrelated history.",
                self.name,
                state_mr.merge_request_id,
                state_mr.source_branch,
                current_ref,
            )
        return publish_mr

    @staticmethod
    def _state_merge_request(state: GitState) -> MergeRequest | None:
        """The checkpointed MR, raising when it didn't revive — see :func:`checkpointed_merge_request`."""
        return checkpointed_merge_request(state, strict=True)

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
        # A dropped MR is not logged here: this hop runs once per model call, and
        # ``_publish_target`` reports the same condition once, at the end of the turn.
        current_ref = request.runtime.context.repo.current_ref
        effective_mr = self._effective_mr(
            cast("GitState", request.state), request.runtime.context, current_ref=current_ref
        )

        runtime_ctx = request.runtime.context
        other_repositories = [
            {
                "repository": handle.repository.slug,
                "path": handle.workspace_path,
                "current_branch": handle.current_ref,
                "default_branch": handle.config.default_branch,
            }
            for handle in runtime_ctx.extra_repos
        ]

        context = {
            "git_platform": request.runtime.context.git_platform.value,
            "repository": request.runtime.context.repository.slug,
            "current_branch": current_ref,
            "default_branch": request.runtime.context.config.default_branch,
            "issue_iid": request.runtime.context.issue.iid if request.runtime.context.issue else None,
            "merge_request_iid": effective_mr.merge_request_id if effective_mr is not None else None,
            "has_other_repositories": bool(other_repositories),
            "other_repositories": other_repositories,
        }
        if sections := [section for handle in runtime_ctx.repos if (section := checkout_section(handle))]:
            context |= {"has_checkout_sections": True, "checkout_sections": "\n\n".join(sections)}

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

    @staticmethod
    async def _alink_related(
        ctx: RuntimeCtx, main_merge_request: MergeRequest | None, repo_publishes: list[RepoPublish]
    ) -> None:
        """Link the session's merge requests to each other, the main one first, each submodule's with the repository
        that records it; a no-op below two of them."""
        handles = {handle.repo_id: handle for handle in ctx.repos}
        handles |= {sub.repo_id: sub for handle in ctx.repos for sub in handle.submodules}
        entries = (
            [RelatedMergeRequest(ctx.repo.client, ctx.repository.slug, main_merge_request)]
            if main_merge_request
            else []
        )
        for publish in repo_publishes:
            handle = handles.get(publish.repo_id)
            if publish.merge_request is None or handle is None:
                continue
            parent = handles.get(publish.parent_repo_id) if publish.parent_repo_id else None
            entries.append(
                RelatedMergeRequest(
                    handle.client,
                    handle.repository.slug,
                    publish.merge_request,
                    parent_repo_id=parent.repository.slug if parent is not None else "",
                )
            )
        await aensure_related_links(entries)

    @staticmethod
    def _kept_out_notices(
        ctx: RuntimeCtx, outcome: PublishOutcome | None, repo_publishes: Sequence[RepoPublish]
    ) -> list[str]:
        """Each repository's kept-out notice, main first, naming the repository when the session has more than one."""
        notices = []
        for root in ctx.repos:
            if root is ctx.repo:
                kept_out = outcome.kept_out if outcome is not None else None
            else:
                own = next((p for p in repo_publishes if p.repo_id == root.repo_id and not p.submodule_path), None)
                kept_out = own.kept_out if own is not None else None
            submodule_publishes = [p for p in repo_publishes if p.submodule_path and p.root_repo_id == root.repo_id]
            where = f" in `{root.repository.slug}`" if ctx.extra_repos else ""
            if notice := kept_out_notice(root, kept_out, submodule_publishes, where=where):
                notices.append(notice)
        return notices

    @staticmethod
    async def _emit_publish_result(payload: dict[str, Any]) -> None:
        try:
            await adispatch_custom_event(PUBLISH_RESULT_EVENT, payload)
        except Exception:
            logger.warning("Could not dispatch the publish result; the run will not show it", exc_info=True)

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
        and the private ``code_changes`` / ``protected_branch_fallback_source`` flags. A merge the agent left with
        conflict markers is not published: the turn ends with a reply naming the files instead of failing.

        The session's extra repositories publish after the main one, each to its own merge request (see
        :func:`~automation.agent.publishers.apublish_extra_repos`), and land in ``repo_publishes`` on every return. With
        extra repositories, a failed main publish is recorded in ``main_publish_error`` and ends the turn with a notice
        rather than raising, which would skip the extra repositories' publishes. Each repository dispatches its own
        ``PUBLISH_RESULT_EVENT``, main first, once every repository has published: the chat ends its publish phase on
        the first one. The list of failed extra repositories is a synthetic message the chat
        hides (it stays in the run's messages); an extra repository's merge conflicts get a visible notice naming
        the files.

        Short-circuited runs (a builtin slash command jumps from ``SlashCommandMiddleware.abefore_agent``
        straight to the after_agent chain) never ran the agent loop, so nothing was captured or changed. Unless
        the command reads the repository, the run executor acquires no sandbox session for it either: its
        workspace is then not ready, and probing git through it would raise (``SandboxFileBackend is not
        bound to a sandbox session``), so this no-ops. A disk workspace is always ready and correctly
        reports a clean tree. The short-circuit also skipped ``abefore_agent``, so this clears the turn's publish
        results itself: the executor would otherwise report, persist and re-arm the previous turn's.
        """
        if not self._workspace.is_ready:
            logger.debug(
                "Workspace not ready at turn end (run short-circuited before the agent loop); "
                "skipping patch capture and publish"
            )
            return _turn_publish_reset()

        update: dict[str, Any] = {}
        if self.capture_patch:
            try:
                update["model_patch"] = await self._workspace.git.get_diff()
            except GitCommandError, httpx.HTTPError, SandboxGitProtocolError:
                # Not publishing (eval harnesses): the patch IS the run's artifact — fail loudly
                # rather than record an empty patch indistinguishable from "agent made no changes".
                if not self.auto_commit_changes:
                    raise
                # Publishing: capture is read-only observability — never strand the agent's work
                # uncommitted over it.
                logger.exception("Patch capture failed; publishing without model_patch")

        if not self.auto_commit_changes:
            return update or None

        try:
            agent_summary = final_assistant_text(state.get("messages", []))
        except Exception:
            # A description sentence is never worth losing the run's work over, and this reads
            # provider-shaped content blocks whose text is not guaranteed to be a string.
            logger.exception("Could not read the run's closing summary; publishing without it")
            agent_summary = None

        ctx = runtime.context
        multi_repo = bool(ctx.extra_repos)
        main_submodules, main_record = await apublish_submodules(
            ctx,
            self._workspace,
            ctx.repo,
            settings=self._settings,
            session_links=self._session_links,
            skip_ci=self.skip_ci,
        )
        tolerant = multi_repo or any(publish.published for publish in main_submodules)
        if tolerant:
            update["main_publish_error"] = None

        publisher = GitChangePublisher(ctx, self._workspace, settings=self._settings, session_links=self._session_links)
        outcome: PublishOutcome | None = None
        notice: str | None = None
        main_payload: dict[str, Any] | None = None
        try:
            outcome = await publisher.publish(
                merge_request=self._publish_target(state, runtime),
                skip_ci=self.skip_ci,
                agent_summary=None if multi_repo else agent_summary,
                scope=PublishScope.from_config(ctx.config.checkout, record=main_record),
            )
        except UnresolvedMergeConflictsError as exc:
            logger.warning("Not publishing: %s", exc)
            if tolerant:
                update["main_publish_error"] = str(exc)
            where = f" in the main repository `{ctx.repository.slug}`" if multi_repo else ""
            notice = _conflicts_notice(exc.paths, where=where)
            main_payload = publish_result_payload(
                status="not_published", error_kind="conflicts", repository=ctx.repo.repo_id
            )
        except Exception as exc:
            main_payload = publish_result_payload(
                status="failed", error_kind=publish_error_kind(exc), repository=ctx.repo.repo_id
            )
            if not tolerant:
                await self._emit_publish_result(main_payload)
                raise
            logger.exception("Publishing to %s failed", ctx.repository.slug)
            update["main_publish_error"] = publish_failure(ctx.repository.slug, exc)
            others = "The other repositories were published" if multi_repo else "Its submodules were published"
            notice = (
                f"I couldn't publish the changes in the main repository `{ctx.repository.slug}` "
                f"({type(exc).__name__}). {others} on their own, and their results stand."
            )
        else:
            if outcome.published:
                main_payload = published_outcome_payload(outcome, repository=ctx.repo.repo_id)
        main_submodules = mark_recorded(main_submodules, main_record, recorded=outcome is not None)

        extra_publishes: list[RepoPublish] = []
        if multi_repo:
            extra_publishes = await apublish_extra_repos(
                ctx, self._workspace, settings=self._settings, session_links=self._session_links, skip_ci=self.skip_ci
            )
        repo_publishes = [*main_submodules, *extra_publishes]
        if multi_repo or repo_publishes:
            update["repo_publishes"] = [publish.to_state() for publish in repo_publishes]
        for payload in [main_payload, *map(repo_publish_payload, repo_publishes)]:
            if payload is not None:
                await self._emit_publish_result(payload)

        # A link block only goes stale when a publish opens a merge request or rewrites its description.
        published_submodule_mr = any(
            publish.submodule_path and publish.published and publish.merge_request is not None
            for publish in repo_publishes
        )
        if (multi_repo or published_submodule_mr) and (
            outcome is None or outcome.published or any(publish.published for publish in repo_publishes)
        ):
            main_merge_request = outcome.merge_request if outcome is not None else None
            await self._alink_related(
                ctx, main_merge_request or checkpointed_merge_request(state, strict=False), repo_publishes
            )
        conflicts, failures = _sibling_notices(ctx, repo_publishes)
        kept_out_notices = self._kept_out_notices(ctx, outcome, repo_publishes)
        messages = []
        if visible_text := "\n\n".join(filter(None, [notice, *conflicts, *kept_out_notices])):
            messages.append(await streamed_assistant_message(visible_text))
        if failures:
            messages.append(synthetic_ai_message(failures, kind=PUBLISH_FAILURES_KIND))
        if messages:
            update["messages"] = messages
        if outcome is None:
            return update

        if outcome.diff_stats is not None:
            update["diff_stats"] = outcome.diff_stats.model_dump()
        if outcome.published:
            # Only ever set true here; ``abefore_agent`` clears it, so a no-op turn cannot
            # inherit the previous turn's push.
            update["published"] = True

        if outcome.merge_request is None:
            # published + no MR is the branch-visibility degrade (branch pushed, MR pending): record
            # that work landed so the run isn't persisted as an indistinguishable no-op.
            if outcome.published:
                update["code_changes"] = True
            return update or None

        self._record_issue_mr(outcome.merge_request, runtime)
        update |= {"merge_request": outcome.merge_request, "code_changes": True}
        if outcome.published:
            update["protected_branch_fallback_source"] = outcome.protected_branch_fallback_source
        return update
