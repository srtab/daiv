from typing import TypedDict

from aequatioai.codebase.models import MergeRequest, RepositoryFile


class Invoke(TypedDict):
    prompt: str | None


class RefactorInvoke(Invoke):
    files_to_change: list[RepositoryFile]
    changes_example_file: RepositoryFile | None


class MergerRequestRefactorInvoke(Invoke):
    repo_id: str
    merge_request: MergeRequest


class ReplacerInvoke(Invoke):
    original_snippet: str
    replacement_snippet: str
    content: str
    commit_message: str
