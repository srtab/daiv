from __future__ import annotations

from django.template import Context, Template


def _render(model: str, thinking_level: str = "") -> str:
    tpl = Template("{% load agent_tags %}{% agent_model_pill model lvl %}")
    return tpl.render(Context({"model": model, "lvl": thinking_level})).strip()


def _visible(rendered: str) -> str:
    # title="…" leaks the raw spec; isolate the inner text for substring checks.
    if not rendered:
        return ""
    return rendered.split(">", 1)[1].rsplit("<", 1)[0]


def test_empty_model_renders_nothing():
    assert _render("") == ""


def test_strips_provider_prefix_and_org_path():
    visible = _visible(_render("openrouter:anthropic/claude-opus-4.6"))
    assert visible.strip() == "claude-opus-4.6"


def test_includes_translated_thinking_label():
    out = _render("openrouter:anthropic/claude-opus-4.6", "high")
    assert "· High" in out


def test_title_keeps_full_spec_and_effort_label():
    out = _render("openrouter:anthropic/claude-opus-4.6", "medium")
    assert 'title="openrouter:anthropic/claude-opus-4.6 · Medium"' in out


def test_truncates_long_model_name():
    # Pill stays compact in dense list layouts; title preserves the full spec.
    visible = _visible(_render("provider:" + "a" * 50))
    assert visible.strip() == "a" * 24


def test_unknown_thinking_level_falls_back_to_empty():
    out = _render("openrouter:claude", "bogus-level")
    assert "·" not in out
    assert 'title="openrouter:claude"' in out
