from __future__ import annotations

import re

_FILLER_PREFIXES = re.compile(
    r"^(?:can you|could you|please|i need|i want|help me|i'd like|i would like)\s+", re.IGNORECASE
)

MAX_TITLE_LENGTH = 120
MAX_HEURISTIC_LENGTH = 80


class TitlerService:
    @classmethod
    def heuristic(cls, prompt: str) -> str:
        """Best-effort title from ``prompt`` without calling an LLM: first non-empty
        line, leading filler prefix stripped, truncated to ``MAX_HEURISTIC_LENGTH``.
        """
        first_line = ""
        for line in prompt.splitlines():
            stripped = line.strip()
            if stripped:
                first_line = stripped
                break

        if not first_line:
            return ""

        title = _FILLER_PREFIXES.sub("", first_line).strip()
        return title[:MAX_HEURISTIC_LENGTH]
