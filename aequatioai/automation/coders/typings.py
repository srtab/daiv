from typing import TypedDict

from codebase.models import RepositoryFile


class Invoke(TypedDict):
    prompt: str | None


class RefactorInvoke(Invoke):
    files_to_change: list[RepositoryFile]
    changes_example_file: RepositoryFile | None


class MergerRequestRefactorInvoke(Invoke):
    target_repo_id: str
    target_ref: str
    source_repo_id: str
    merge_request_id: str


class ReplacerInvoke(Invoke):
    original_snippet: str
    replacement_snippet: str
    content: str
    commit_message: str
