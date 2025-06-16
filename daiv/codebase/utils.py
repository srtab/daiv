import fnmatch
import re
from collections.abc import Iterable
from pathlib import Path

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from codebase.base import Discussion, Note, User
from core.constants import BOT_NAME


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
    # Check for explicit user mention (case-insensitive)
    mention_pattern = rf"@{re.escape(current_user.username)}\b"
    if re.search(mention_pattern, note_body, re.IGNORECASE):
        return True

    # Check for bare textual reference (case-insensitive)
    return bool(re.search(r"\bDAIV\b", note_body, re.IGNORECASE))


def discussion_has_daiv_notes(discussion: Discussion, current_user: User) -> bool:
    """
    Check if the discussion has any notes authored by DAIV.

    Args:
        discussion: The discussion to check
        current_user: The current DAIV user

    Returns:
        bool: True if any note in the discussion is authored by DAIV, False otherwise
    """
    return any(note.author.id == current_user.id for note in discussion.notes)


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


class RepoAnalyzer:
    def __init__(self, root_path: Path, exclude_patterns: Iterable[str] | None = None):
        """
        Initialize the repository analyzer.

        Args:
            root_path: Path to the repository root
            exclude_patterns: List of glob patterns to exclude (e.g., ['*.pyc', '__pycache__', 'node_modules'])
        """
        self.root_path = root_path
        self.exclude_patterns = exclude_patterns or []

    def should_exclude(self, path: Path) -> bool:
        """
        Check if a path should be excluded based on patterns.

        Args:
            path: Path to check

        Returns:
            True if the path should be excluded, False otherwise
        """
        return any(fnmatch.fnmatch(str(path), pattern) for pattern in self.exclude_patterns)

    def analyze_repo(self) -> Iterable[str]:
        """
        Analyze the repository and return a structured representation.

        Returns:
            A dictionary containing the repository structure.
        """
        file_paths = []

        for root, _, files in self.root_path.walk():
            if self.should_exclude(root):
                continue

            for file in sorted(files):
                full_path = Path(root) / file
                if self.should_exclude(full_path):
                    continue

                file_paths.append(str(full_path.relative_to(self.root_path)))

        return sorted(file_paths)


def analyze_repository(repo_path: Path | str, exclude_patterns: Iterable[str] | None = None) -> str:
    """
    Analyze a repository and return a clean, formatted string representation suitable for LLMs.

    Args:
        repo_path: Path to the repository
        exclude_patterns: List of glob patterns to exclude

    Returns:
        A formatted string representation of the repository structure
    """
    if isinstance(repo_path, str):
        repo_path = Path(repo_path)

    analyzer = RepoAnalyzer(repo_path, exclude_patterns)
    repo_structure = analyzer.analyze_repo()

    return "\n".join(repo_structure)
