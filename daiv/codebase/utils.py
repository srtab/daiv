import contextlib
import fnmatch
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from git import GitCommandError
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from unidiff import PatchSet
from unidiff.constants import LINE_TYPE_CONTEXT
from unidiff.patch import Line

from core.constants import BOT_NAME

if TYPE_CHECKING:
    from git import Repo

    from codebase.base import Discussion, Note, User


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


class GitManager:
    """
    Manager for interacting with a Git repository.

    Args:
        repo: The repository to interact with.
    """

    BRANCH_NAME_MAX_ATTEMPTS = 10

    def __init__(self, repo: Repo):
        """
        Initialize the Git repository manager.

        Args:
            repo: The repository to interact with.
        """
        self.repo = repo

    def get_diff(self) -> str:
        """
        Get the diff of the repository's including unstaged changes.

        Returns:
            The diff of the repository.
        """
        try:
            self.repo.git.add("-A")
            return self.repo.git.diff("--cached", "HEAD")
        finally:
            # Always unstage changes, even if something goes wrong
            self.repo.git.reset("HEAD")

    def is_dirty(self) -> bool:
        """
        Check if the repository is dirty.

        Returns:
            True if the repository is dirty, False otherwise.
        """
        return self.repo.is_dirty(index=True, working_tree=True, untracked_files=True)

    def commit_changes(
        self,
        commit_message: str,
        *,
        branch_name: str,
        skip_ci: bool = False,
        override_commits: bool = False,
        use_branch_if_exists: bool = True,
    ) -> str:
        """
        Commit the changes to the repository.

        Args:
            commit_message: The commit message.
            branch_name: The branch name to commit the changes to.
            skip_ci: Whether to skip the CI.
            override_commits: Whether to override existing commits.
            use_branch_if_exists: Whether to use the branch if it exists or generate a unique branch name.

        Returns:
            The branch name.
        """
        self.repo.remotes.origin.fetch()

        existing_branch_names = [head.name for head in self.repo.heads]

        if branch_name in existing_branch_names:
            if override_commits and use_branch_if_exists:
                self.repo.delete_head(branch_name)
                self.repo.create_head(branch_name)

            elif not use_branch_if_exists:
                branch_name = self._gen_unique_branch_name(branch_name, existing_branch_names)
                self.repo.create_head(branch_name)
        else:
            self.repo.create_head(branch_name)

        self.repo.heads[branch_name].checkout()

        self.repo.git.add("-A")
        self.repo.index.commit(commit_message if not skip_ci else f"[skip ci] {commit_message}")
        self.repo.remotes.origin.push(branch_name, force=override_commits)
        return branch_name

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

    def apply_patch(self, patch: str):
        """
        Apply a patch to the repository.

        Args:
            patch: The patch to apply.
        """
        if not patch or not patch.strip():
            return

        diff_bytes = patch.encode("utf-8", "surrogateescape")
        diff_args = ["--whitespace=nowarn"]

        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp:
            tmp.write(diff_bytes)
            tmp.flush()
            tmp_path = tmp.name

        try:
            try:
                self.repo.git.apply(*diff_args, "--check", tmp_path)
            except GitCommandError as e:
                # Check if the error is about empty/invalid patches
                if "No valid patches in input" in str(e):
                    # Empty or invalid patch - this is not an error, just skip it
                    return
                raise RuntimeError("git apply failed. The patch is not valid.") from e

            self.repo.git.apply(*diff_args, tmp_path)
        finally:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
