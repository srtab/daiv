from __future__ import annotations

from skills.services import list_builtins


def test_list_builtins_returns_dicts_with_name_and_description():
    builtins = list_builtins()
    assert builtins, "expected at least one shipped built-in skill"
    sample = builtins[0]
    assert set(sample.keys()) >= {"name", "description"}
    # Sanity: every name is a non-empty string
    for entry in builtins:
        assert entry["name"]
        # Description may be empty if a built-in lacks frontmatter, but it
        # should always be a string.
        assert isinstance(entry["description"], str)
