from collections.abc import Iterator
from typing import TypedDict

from codebase.base import RepositoryFile


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


class PathsExtractorInvoke(Invoke):
    code_snippet: str
    repository_tree: Iterator[str]


class ChangeDescriberInvoke(Invoke):
    changes: list[str]
