from __future__ import annotations

from chat.turns import build_turns


def test_build_turns_empty_list_returns_empty():
    assert build_turns([]) == []
