import pytest

from automation.picker.display import MODEL_NAME_MAX_LEN, display_model_name, display_thinking_level


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("openrouter:anthropic/claude-haiku-4.5", "claude-haiku-4.5"),
        ("anthropic:claude-opus-4-6", "claude-opus-4-6"),
        ("claude-opus-4-5", "claude-opus-4-5"),
        ("openrouter:meta-llama/llama-3.3-70b-instruct", "llama-3.3-70b-instruct"),
        ("", ""),
        # Empty model name after the colon — trailing-slash fallback keeps both sides
        # returning "" rather than the raw "provider:" prefix.
        ("provider:", ""),
        # Trailing slash with empty leaf — the `or name` fallback in the helper means
        # we return the un-split remainder ("org/") rather than "", and the JS mirror
        # must do the same.
        ("provider:org/", "org/"),
    ],
)
def test_display_model_name_normalisation(spec, expected):
    """Pin the exact spec→display normalisation. The JS picker mirrors this in
    ``automation/static/automation/js/agent-picker.js`` (``shortenModel`` and the
    inline ``modelName.split('/').pop()`` in ``pillLabel``) so the locked composer
    pill — driven by the picker's ``daiv:agent-changed`` event — shows the same
    name as a server-rendered persisted thread. Update both sides together when
    this table changes."""
    assert display_model_name(spec) == expected


def test_display_model_name_truncates_when_max_len_set():
    """``max_len`` is opt-in; consumers that render in tight layouts (gallery, pill)
    pass ``MODEL_NAME_MAX_LEN`` to share the cap. Without the kwarg, the full
    normalised name flows through so callers can format their own."""
    long_spec = "provider:vendor/" + "x" * 50
    assert display_model_name(long_spec) == "x" * 50
    assert display_model_name(long_spec, max_len=MODEL_NAME_MAX_LEN) == "x" * MODEL_NAME_MAX_LEN


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("minimal", "Minimal"),
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("xhigh", "Extra high"),
        ("", ""),
        ("bogus", ""),
    ],
)
def test_display_thinking_level(level, expected):
    """Translation-aware label lookup. Unknown values (renamed enum members,
    legacy data) must fall back to empty rather than leaking the raw key."""
    assert display_thinking_level(level) == expected
