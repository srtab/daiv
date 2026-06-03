import fnmatch
import logging
import re
import subprocess  # noqa: S404
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from unidiff import PatchSet
from unidiff.constants import LINE_TYPE_CONTEXT
from unidiff.patch import Line

from core.constants import BOT_NAME
from core.utils import generate_uuid

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

    from git import Repo

    from codebase.base import Discussion, Note, Scope, User


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
