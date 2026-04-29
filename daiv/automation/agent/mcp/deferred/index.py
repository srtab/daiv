from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rank_bm25 import BM25Plus

if TYPE_CHECKING:
    from collections.abc import Iterable

    from langchain_core.tools import BaseTool

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({"the", "a", "an", "of", "to", "for", "with", "in", "on", "by", "is", "and", "or"})
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


class DeferredMCPToolsIndex:
    def __init__(self, tools: Iterable[BaseTool], always_loaded: Iterable[str] = ()) -> None:
        self._entries: dict[str, ToolEntry] = {}
        self._always_loaded_names: set[str] = set(always_loaded)

        for tool in tools:
            self._entries[tool.name] = self._build_entry(tool)

        unknown = self._always_loaded_names - self._entries.keys()
        if unknown:
            raise ValueError(f"always_loaded references unknown tool names: {sorted(unknown)}")

        self._names: list[str] = list(self._entries.keys())
        self._always_loaded_tools: list[BaseTool] = [
            self._entries[n].tool for n in self._names if n in self._always_loaded_names
        ]
        self._deferred_entries: list[ToolEntry] = [
            entry for name, entry in self._entries.items() if name not in self._always_loaded_names
        ]
        if self._names:
            self._bm25 = BM25Plus([_tokenize(self._entries[n].indexed_text) for n in self._names])
        else:
            self._bm25 = None

    def get(self, name: str) -> ToolEntry | None:
        return self._entries.get(name)

    def search(self, query: str, top_k: int = 5) -> list[ToolEntry]:
        if not query.strip() or self._bm25 is None or top_k <= 0:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(zip(scores, self._names, strict=True), key=lambda pair: pair[0], reverse=True)
        return [self._entries[name] for score, name in ranked[:top_k] if score > 0]

    def always_loaded_tools(self) -> list[BaseTool]:
        return self._always_loaded_tools

    def always_loaded_names(self) -> set[str]:
        return self._always_loaded_names

    def deferred_entries(self) -> list[ToolEntry]:
        return self._deferred_entries

    @staticmethod
    def _build_entry(tool: BaseTool) -> ToolEntry:
        name_text = re.sub(r"_+", " ", tool.name)

        arg_text = ""
        if tool.args_schema is not None:
            schema = tool.args_schema.model_json_schema() if hasattr(tool.args_schema, "model_json_schema") else {}
            properties = schema.get("properties") or {}
            arg_text = " ".join(f"{key} {value.get('description', '')}" for key, value in properties.items())

        description = tool.description or ""
        indexed_text = f"{name_text} {description} {arg_text}"[:_INDEXED_TEXT_CAP]
        summary = (description.splitlines() or [""])[0][:_SUMMARY_CAP]

        return ToolEntry(name=tool.name, tool=tool, indexed_text=indexed_text, summary=summary)
