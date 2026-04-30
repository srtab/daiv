from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic.errors import PydanticInvalidForJsonSchema
from rank_bm25 import BM25Plus

if TYPE_CHECKING:
    from collections.abc import Iterable

    from langchain_core.tools import BaseTool

logger = logging.getLogger("daiv.tools")

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset((Path(__file__).parent / "stopwords.txt").read_text().split())
_INDEXED_TEXT_CAP = 2048
_SUMMARY_CAP = 200


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


@dataclass(frozen=True)
class ToolEntry:
    name: str
    tool: BaseTool
    indexed_text: str
    summary: str


class DeferredToolsIndex:
    """BM25 index over deferred tools — every tool given to it is treated as deferred."""

    def __init__(self, tools: Iterable[BaseTool]) -> None:
        self._entries: dict[str, ToolEntry] = {}
        for tool in tools:
            if tool.name in self._entries:
                continue
            self._entries[tool.name] = self._build_entry(tool)

        self._names: list[str] = list(self._entries.keys())
        self._tokenized: list[list[str]] = [_tokenize(self._entries[n].indexed_text) for n in self._names]
        self._token_sets: list[frozenset[str]] = [frozenset(t) for t in self._tokenized]
        self._bm25 = BM25Plus(self._tokenized) if self._names else None

    def get(self, name: str) -> ToolEntry | None:
        return self._entries.get(name)

    def search(self, query: str, top_k: int = 5) -> list[ToolEntry]:
        if not query.strip() or self._bm25 is None or top_k <= 0:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        # BM25Plus assigns a non-zero baseline to every doc, and on small tool corpora the IDF
        # is flat — a doc that matches one query token can outscore a doc with no overlap by less
        # than 2x. Filter to entries tied for the highest query-token coverage, then let BM25
        # rank within that tier. This keeps single-word queries working (max coverage = 1) while
        # cutting weak partial matches when richer matches exist (e.g. "gitlab merge request" hits
        # gitlab on 2 tokens, drops sentry_list_* which only shares "list").
        query_tokens = set(tokens)
        scores = self._bm25.get_scores(tokens)
        with_overlap = [
            (score, name, len(doc_tokens & query_tokens))
            for score, name, doc_tokens in zip(scores, self._names, self._token_sets, strict=True)
            if doc_tokens & query_tokens
        ]
        if not with_overlap:
            return []
        max_overlap = max(coverage for _, _, coverage in with_overlap)
        ranked = sorted(
            ((score, name) for score, name, coverage in with_overlap if coverage >= max_overlap),
            key=lambda t: t[0],
            reverse=True,
        )
        return [self._entries[name] for _, name in ranked[:top_k]]

    def deferred_entries(self) -> list[ToolEntry]:
        return list(self._entries.values())

    def names(self) -> list[str]:
        return list(self._names)

    @staticmethod
    def _build_entry(tool: BaseTool) -> ToolEntry:
        name_text = re.sub(r"_+", " ", tool.name)

        arg_text = ""
        if tool.args_schema is not None and hasattr(tool.args_schema, "model_json_schema"):
            try:
                schema = tool.args_schema.model_json_schema()
            except PydanticInvalidForJsonSchema:
                # Tools whose args reference non-pydantic types (e.g. git.Repo) can't emit a JSON
                # schema; fall back to name+description for indexing.
                logger.debug("deferred-index: skipping arg schema for %s (non-serializable)", tool.name)
                schema = {}
            properties = schema.get("properties") or {}
            arg_text = " ".join(f"{key} {value.get('description', '')}" for key, value in properties.items())

        description = tool.description or ""
        indexed_text = f"{name_text} {description} {arg_text}"[:_INDEXED_TEXT_CAP]
        summary = (description.splitlines() or [""])[0][:_SUMMARY_CAP]

        return ToolEntry(name=tool.name, tool=tool, indexed_text=indexed_text, summary=summary)
