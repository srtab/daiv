from collections.abc import Iterator
from typing import TypedDict

from codebase.base import Note


class Invoke(TypedDict):
    prompt: str | None


class CodebaseInvoke(Invoke):
    source_repo_id: str
    source_ref: str


class ReviewAddressorInvoke(CodebaseInvoke):
    file_path: str
    notes: list[Note]
    diff: str | None = None


class MergerRequestRefactorInvoke(Invoke):
    target_repo_id: str
    target_ref: str
    source_repo_id: str
    merge_request_id: str


class ReplacerInvoke(Invoke):
    original_snippet: str
    replacement_snippet: str
    content: str


class PathsReplacerInvoke(Invoke):
    code_snippet: str
    repository_tree: Iterator[str]


class ChangesDescriberInvoke(Invoke):
    changes: list[str]
