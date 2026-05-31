import asyncio
import fnmatch
import logging
import re
import subprocess  # noqa: S404
from dataclasses import dataclass
from typing import TYPE_CHECKING

from git import GitCommandError
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from unidiff import PatchSet
from unidiff.constants import LINE_TYPE_CONTEXT
from unidiff.errors import UnidiffParseError
from unidiff.patch import Line

from core.constants import BOT_NAME
from core.sandbox.schemas import RunCommandsRequest
from core.utils import generate_uuid

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

    from git import Repo

    from codebase.base import Discussion, Note, Scope, User
    from core.sandbox.client import DAIVSandboxClient


def compute_thread_id(*, repo_slug: str, scope: Scope, entity_iid: int | str) -> str:
    """Deterministic LangGraph checkpoint key for an issue or merge-request conversation.

    Webhook callbacks mint this to set ``Activity.thread_id`` before enqueueing the
    addressor task; the addressor managers must compute the same value so follow-up
    events resume the same checkpointer state.
    """
    if not repo_slug or scope is None or entity_iid is None or entity_iid == "":
        raise ValueError(
            f"compute_thread_id requires non-empty values; "
            f"got repo_slug={repo_slug!r}, scope={scope!r}, entity_iid={entity_iid!r}"
        )
    return generate_uuid(f"{repo_slug}:{scope}/{entity_iid}")


def get_repo_ref(repo: Repo) -> str:
    """
    Get the current reference (branch name or commit SHA) from a repository.

    When HEAD is attached to a branch, returns the branch name.
    When HEAD is detached (e.g., checking out a specific commit), returns the commit SHA.

    Args:
        repo: The Git repository object.

    Returns:
        The branch name if HEAD is attached, or the commit SHA if HEAD is detached.
    """
    try:
        return repo.active_branch.name
    except TypeError:
        # HEAD is detached, return the commit SHA
        return repo.head.commit.hexsha


def note_mentions_daiv(note_body: str, current_user: User) -> bool:
    """
    Check if the note body references the DAIV GitLab account.

    Returns True when the note contains:
    - Explicit user-mention (e.g. @daiv, @DAIV)
    - Bare textual reference to the bot name (e.g. DAIV please fix)

    Args:
        note_body: The note body text to check
        current_user: The current DAIV user

    Returns:
        bool: True if the note mentions DAIV, False otherwise
    """
    mention_pattern = rf"@{re.escape(current_user.username)}\b"
    return bool(re.search(mention_pattern, note_body, re.IGNORECASE))


def discussion_has_daiv_mentions(discussion: Discussion, current_user: User) -> bool:
    """
    Check if the discussion has any notes mentioning DAIV.

    Args:
        discussion: The discussion to check
        current_user: The current DAIV user

    Returns:
        bool: True if any note in the discussion mentions DAIV, False otherwise
    """
    return any(note_mentions_daiv(note.body, current_user) for note in discussion.notes)


def notes_to_messages(notes: list[Note], bot_user_id) -> list[AnyMessage]:
    """
    Convert a list of notes to a list of messages.

    Args:
        notes: List of notes
        bot_user_id: ID of the bot user

    Returns:
        List of messages
    """
    messages: list[AnyMessage] = []
    for note in notes:
        if note.author.id == bot_user_id:
            messages.append(AIMessage(id=note.id, content=note.body, name=BOT_NAME))
        else:
            messages.append(HumanMessage(id=note.id, content=note.body, name=note.author.username))
    return messages


def redact_diff_content(
    diff: str, omit_content_patterns: tuple[str, ...], as_patch_set: bool = False
) -> str | PatchSet:
    """
    Redact the diff content of the file that are marked as omit_content_patterns.

    Args:
        diff: The diff to redact.
        omit_content_patterns: The patterns to omit from the diff.
        as_patch_set: Whether to return the diff as a PatchSet.

    Returns:
        The redacted diff as a string or a PatchSet.
    """
    patch_set = PatchSet.from_string(diff)

    for patch_file in patch_set:
        for hunk in patch_file:
            if any(fnmatch.fnmatch(patch_file.path, pattern) for pattern in omit_content_patterns):
                hunk.clear()
                hunk.append(
                    Line("[Diff content was intentionally excluded by the repository configuration]", LINE_TYPE_CONTEXT)
                )
    return str(patch_set) if not as_patch_set else patch_set


def files_changed_from_patch(patch: str | None) -> list[dict[str, str]]:
    """Derive a ``{path, op[, from_path]}`` list from a unified diff.

    The sandbox reports every workspace mutation regardless of how it happened
    (bash ``rm``/``mv``, scripts, ``find -delete``, …), which is what lets the
    chat rail surface them alongside ``edit_file``/``write_file`` tool calls.
    """
    if not patch or not patch.strip():
        return []
    try:
        patch_set = PatchSet.from_string(patch)
    except UnidiffParseError:
        logger.warning("Failed to parse patch for files_changed", exc_info=True)
        return []

    files: list[dict[str, str]] = []
    for patched_file in patch_set:
        entry: dict[str, str] = {"path": patched_file.path}
        if patched_file.is_added_file:
            entry["op"] = "added"
        elif patched_file.is_removed_file:
            entry["op"] = "deleted"
        elif patched_file.is_rename:
            entry["op"] = "renamed"
            entry["from_path"] = patched_file.source_file.removeprefix("a/").removeprefix("b/")
        else:
            entry["op"] = "modified"
        files.append(entry)
    return files


_SHELL_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./@=:,+-]+$")


def _shell_quote(arg: str) -> str:
    """POSIX single-quote ``arg`` unless it is already shell-safe.

    Flags/refs/paths (``status``, ``--porcelain``, ``origin/main..HEAD``) pass through
    unquoted for readability; anything with spaces or shell metacharacters (commit
    messages, ``--format=%(...)``) is single-quoted so the sandbox shell sees it verbatim.
    """
    if arg and _SHELL_SAFE_ARG.match(arg):
        return arg
    return "'" + arg.replace("'", "'\\''") + "'"


@dataclass
class _GitResult:
    """Normalized result of one git invocation (sandbox or local)."""

    exit_code: int
    output: str


class GitManager:
    """Run git operations against a repository in one of two mutually-exclusive modes:

    - **Sandbox mode** (``client`` + ``session_id``): git runs *in the sandbox* via
      ``run_commands`` in ``repo_path`` (``/workspace/repo``). Used for sandbox-enabled
      runs, where the agent's changes are sandbox-authoritative (no local copy).
    - **Local mode** (``repo``): git runs as a subprocess against a GitPython clone's
      working tree. Used for sandbox-disabled / repoless runs, where changes live on disk.

    Exactly one mode must be configured. Every operation is async; local-mode git runs in
    a worker thread so the event loop is never blocked.

    Args:
        repo: GitPython repo for local mode.
        client: Sandbox client for sandbox mode.
        session_id: Sandbox session id (required with ``client``).
        repo_path: Repo path inside the sandbox (defaults to ``REPO_PATH``).
    """

    BRANCH_NAME_MAX_ATTEMPTS = 10

    def __init__(
        self,
        repo: Repo | None = None,
        *,
        client: DAIVSandboxClient | None = None,
        session_id: str | None = None,
        repo_path: str | None = None,
    ) -> None:
        if (repo is None) == (client is None):
            raise ValueError("GitManager requires exactly one of `repo` (local) or `client` (sandbox).")
        if client is not None and not session_id:
            raise ValueError("GitManager sandbox mode requires a non-empty session_id.")
        if repo_path is None:
            # Imported lazily to avoid a codebase→automation import-time dependency.
            from automation.agent.constants import REPO_PATH

            repo_path = REPO_PATH
        self.repo = repo
        self._client = client
        self._session_id = session_id
        self._repo_path = repo_path

    # -- git invocation ------------------------------------------------------
    async def _git(self, *args: str, check: bool = True) -> _GitResult:
        """Run one git command in the repo. Raises ``GitCommandError`` on a non-zero
        exit when ``check`` is True; otherwise returns the result for the caller to inspect.
        """
        result = await (self._git_sandbox(args) if self._client is not None else self._git_local(args))
        if check and result.exit_code != 0:
            raise GitCommandError(["git", *args], result.exit_code, result.output)
        return result

    async def _git_sandbox(self, args: tuple[str, ...]) -> _GitResult:
        client, session_id = self._client, self._session_id
        if client is None or session_id is None:  # pragma: no cover - guaranteed by __init__
            raise RuntimeError("GitManager is not in sandbox mode")
        command = " ".join(_shell_quote(token) for token in ("git", "-C", self._repo_path, *args))
        response = await client.run_commands(session_id, RunCommandsRequest(commands=[command], fail_fast=True))
        result = response.results[0]
        return _GitResult(exit_code=result.exit_code, output=result.output)

    async def _git_local(self, args: tuple[str, ...]) -> _GitResult:
        repo = self.repo
        if repo is None:  # pragma: no cover - guaranteed by __init__
            raise RuntimeError("GitManager is not in local mode")

        def _run() -> _GitResult:
            proc = subprocess.run(  # noqa: S603
                ["git", "-C", repo.working_dir, *args],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            return _GitResult(exit_code=proc.returncode, output=proc.stdout + proc.stderr)

        return await asyncio.to_thread(_run)

    # -- queries -------------------------------------------------------------
    async def is_dirty(self) -> bool:
        """Whether the working tree has uncommitted changes (tracked or untracked)."""
        return bool((await self._git("status", "--porcelain")).output.strip())

    async def get_diff(self, ref: str = "HEAD") -> str:
        """Diff against ``ref``, including untracked files (via ``ls-files`` + ``diff --no-index``)."""
        diff = (await self._git("diff", ref, check=False)).output
        untracked = (await self._git("ls-files", "--others", "--exclude-standard", check=False)).output
        for file in (line.strip() for line in untracked.splitlines() if line.strip()):
            # `git diff --no-index` exits 1 when differences are found; we still want the output.
            file_diff = (await self._git("diff", "--no-index", "/dev/null", file, check=False)).output
            if file_diff:
                diff += f"\n{file_diff}"
        if diff and not diff.endswith("\n"):
            diff += "\n"
        return diff

    async def has_unpushed(self, branch: str) -> bool:
        """Whether local HEAD has commits not present on ``origin/<branch>``."""
        out = (await self._git("log", f"origin/{branch}..HEAD", "--oneline", check=False)).output
        return bool(out.strip())

    # -- mutations -----------------------------------------------------------
    async def commit_and_push_changes(
        self,
        commit_message: str,
        *,
        branch_name: str,
        skip_ci: bool = False,
        override_commits: bool = False,
        use_branch_if_exists: bool = True,
    ) -> str:
        """Stage all changes, commit, and push ``branch_name`` to ``origin``.

        Mirrors the prior GitPython semantics: branch-exists (local/remote) handling,
        ``override_commits`` force-recreate, unique-name generation when
        ``use_branch_if_exists`` is False, and a typed ``GitPushPermissionError`` on auth
        failures. Returns the branch name actually pushed to.
        """
        await self._git("fetch", "origin", check=False)

        local_branch_names = self._parse_local_branches(
            (await self._git("branch", "--format=%(refname:short)", check=False)).output
        )
        remote_branch_names = self._parse_remote_branches(
            (await self._git("ls-remote", "--heads", "origin", check=False)).output
        )

        branch_exists_locally = branch_name in local_branch_names
        branch_exists_remotely = branch_name in remote_branch_names

        if branch_exists_locally or branch_exists_remotely:
            if override_commits and use_branch_if_exists:
                if branch_exists_locally:
                    await self._git("branch", "-D", branch_name)  # Force delete local
                await self._git("checkout", "-b", branch_name)  # Create and checkout
            elif not use_branch_if_exists:
                # Need both local and remote for unique-name generation.
                all_branch_names = list(set(local_branch_names + remote_branch_names))
                branch_name = self._gen_unique_branch_name(branch_name, all_branch_names)
                await self._git("checkout", "-b", branch_name)
            else:
                # Branch exists, just checkout (git handles remote tracking).
                await self._git("checkout", branch_name)
        else:
            await self._git("checkout", "-b", branch_name)

        await self._git("add", "-A")
        await self._git("commit", "-m", commit_message if not skip_ci else f"[skip ci] {commit_message}")

        push_args = ["push", "origin", branch_name, *(["--force"] if override_commits else [])]
        push = await self._git(*push_args, check=False)
        if push.exit_code != 0:
            if _is_push_auth_error_text(push.output):
                raise GitPushPermissionError(
                    "Failed to push changes to the remote repository due to authentication or permission issues."
                )
            raise GitCommandError(["git", *push_args], push.exit_code, push.output)

        return branch_name

    async def checkout(self, branch_name: str) -> None:
        """Fetch and checkout ``branch_name``; raise ``ValueError`` if it does not exist."""
        await self._git("fetch", "origin", check=False)
        result = await self._git("checkout", branch_name, check=False)
        if result.exit_code != 0:
            raise ValueError(f"Branch {branch_name} does not exist in the repository.")

    async def commit_all(self, message: str) -> None:
        """Stage every change and commit it. Callers should ensure the tree is dirty first
        (``git commit`` exits non-zero on an empty index)."""
        await self._git("add", "-A")
        await self._git("commit", "-m", message)

    async def push_head_to(self, branch: str, *, force: bool = False) -> str:
        """Push the current ``HEAD`` to ``origin/<branch>`` (creating it if needed).

        Raises ``GitPushPermissionError`` on an auth/permission failure and
        ``GitCommandError`` on any other push failure. Returns ``branch``.
        """
        push_args = ["push", "origin", f"HEAD:{branch}", *(["--force"] if force else [])]
        push = await self._git(*push_args, check=False)
        if push.exit_code != 0:
            if _is_push_auth_error_text(push.output):
                raise GitPushPermissionError(
                    "Failed to push changes to the remote repository due to authentication or permission issues."
                )
            raise GitCommandError(["git", *push_args], push.exit_code, push.output)
        return branch

    async def remote_branches(self) -> list[str]:
        """Branch names that currently exist on ``origin``."""
        return self._parse_remote_branches((await self._git("ls-remote", "--heads", "origin", check=False)).output)

    def unique_branch_name(self, original_branch_name: str, existing_branch_names: list[str]) -> str:
        """Public wrapper over :meth:`_gen_unique_branch_name` for callers outside this class."""
        return self._gen_unique_branch_name(original_branch_name, existing_branch_names)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _parse_local_branches(output: str) -> list[str]:
        """Branch names from ``git branch --format=%(refname:short)``."""
        return [line.strip() for line in output.splitlines() if line.strip()]

    @staticmethod
    def _parse_remote_branches(output: str) -> list[str]:
        """Branch names from ``git ls-remote --heads origin`` (lines: ``<sha>\\trefs/heads/<branch>``)."""
        branches: list[str] = []
        for line in output.splitlines():
            ref = line.strip().split("\t")[-1]
            if ref.startswith("refs/heads/"):
                branches.append(ref[len("refs/heads/") :])
        return branches

    def _gen_unique_branch_name(
        self, original_branch_name: str, existing_branch_names: list[str], max_attempts: int = BRANCH_NAME_MAX_ATTEMPTS
    ) -> str:
        """
        Generate a unique branch name.

        Args:
            original_branch_name: The original branch name.
            existing_branch_names: The existing branch names.
            max_attempts: The maximum number of attempts to generate a unique branch name.

        Returns:
            A unique branch name.
        """
        suffix_count = 1
        branch_name = original_branch_name

        while branch_name in existing_branch_names and suffix_count < max_attempts:
            branch_name = f"{original_branch_name}-{suffix_count}"
            suffix_count += 1

        if suffix_count == max_attempts:
            raise ValueError(
                f"Failed to generate a unique branch name for {original_branch_name}, "
                f"max attempts reached {max_attempts}."
            )

        return branch_name


def apply_patch_to_dir(patch: str, working_dir: Path) -> None:
    """Apply a unified diff to ``working_dir`` using ``git apply``.

    ``git apply`` does not require a ``.git`` directory and is transactional within a
    single invocation, so one subprocess call covers both repo-bound and repoless
    callers. The ``"No valid patches in input"`` stderr line is git's signal for an
    empty or no-op patch and is treated as success.
    """
    if not patch or not patch.strip():
        return

    if not patch.endswith("\n"):
        patch += "\n"

    result = subprocess.run(  # noqa: S603
        ["git", "apply", "--whitespace=nowarn", "-"],  # noqa: S607
        cwd=working_dir,
        input=patch.encode("utf-8", "surrogateescape"),
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return

    stderr = result.stderr.decode("utf-8", "replace").strip()
    stdout = result.stdout.decode("utf-8", "replace").strip()
    if "No valid patches in input" in stderr:
        logger.debug("apply_patch_to_dir: empty/no-op patch, skipping (cwd=%s, stderr=%r)", working_dir, stderr)
        return
    detail = stderr or stdout or "<no diagnostic output>"
    raise RuntimeError(f"git apply failed (rc={result.returncode}, cwd={working_dir}): {detail}")


class GitPushPermissionError(RuntimeError):
    """
    Raised when pushing changes fails due to authentication or permission issues.
    """


def _is_push_auth_error_text(output: str) -> bool:
    """
    Check if git push output indicates an authentication or permission failure.
    """
    text = output.lower()
    return any(
        marker in text
        for marker in (
            "returned error: 403",
            "authentication failed",
            "permission denied",
            "access denied",
            "http basic: access denied",
            "could not read username",
            "not authorized",
        )
    )
