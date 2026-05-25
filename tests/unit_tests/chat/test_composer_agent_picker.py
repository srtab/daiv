"""Tests for the agent model + thinking-effort picker on the chat composer.

The picker mirrors the env picker's two-mode shape:

- on the empty-state hero (no thread yet) it renders as an interactive Alpine
  component with hidden inputs the JS reads at submit time;
- on an existing thread (any thread row, since ``ChatThread`` rows are created
  on the first turn) it renders locked because the backend pins ``agent_model``
  / ``agent_thinking_level`` on first sight and ignores client values afterwards.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.urls import reverse

import pytest

from chat.models import ChatThread
from core.models import Provider, ProviderType


@pytest.fixture
def enabled_provider(db):
    """Ensure at least one enabled provider so the picker renders its dropdown."""
    Provider.objects.filter(slug="openrouter").delete()
    Provider.objects.create(
        slug="openrouter",
        display_name="OpenRouter",
        provider_type=ProviderType.OPENROUTER,
        api_key="sk-test",
        is_enabled=True,
    )


@pytest.mark.django_db
def test_hero_renders_interactive_agent_picker(member_client, enabled_provider):
    """The empty ``chat_new`` page exposes the picker as an editable Alpine root."""
    resp = member_client.get(reverse("chat_new"))
    assert resp.status_code == 200
    body = resp.content.decode()

    # Alpine root marker — same shape as env picker's ``envPicker(`` smoke check.
    assert "agentPicker(" in body
    # Picker context vars made it to the template.
    assert "agent_picker_providers" not in body  # consumed, not literal
    providers = json.loads(resp.context["agent_picker_providers"])
    assert any(p["slug"] == "openrouter" for p in providers)
    # The hero's editable picker mounts its hidden inputs.
    assert 'name="agent_model"' in body
    assert 'name="agent_thinking_level"' in body


@pytest.mark.django_db
def test_existing_thread_renders_locked_agent_pill(member_client, member_user, enabled_provider):
    """Any persisted ``ChatThread`` makes the picker render locked, mirroring env."""
    thread = ChatThread.objects.create(
        thread_id="t-agent",
        user=member_user,
        repo_id="a/b",
        ref="main",
        agent_model="openrouter:anthropic/claude-haiku-4.5",
        agent_thinking_level="medium",
    )
    tup = MagicMock(checkpoint={"channel_values": {"messages": []}})
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.views.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))

    assert resp.status_code == 200
    body = resp.content.decode()
    # The locked pill renders the stripped display form (provider prefix and ``org/``
    # stripped) so the chip stays compact — same normalisation the editable picker
    # applies to pinned models via ``pillLabel``.
    assert "claude-haiku-4.5" in body
    # The locked-mode partial emits an ``aria-disabled`` pill (no Alpine root). The hero
    # picker still renders unconditionally inside its ``<template x-if>`` (Alpine hides
    # it client-side); both share the same partial, so we look for the locked sentinel
    # to confirm at least one render took the disabled branch.
    assert 'aria-disabled="true"' in body
    # Context still carries the raw spec (used to seed the editable picker) plus the
    # pre-computed display form (used by the locked pill).
    assert resp.context["agent_picker_initial_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert resp.context["agent_picker_initial_model_display"] == "claude-haiku-4.5"
    assert resp.context["agent_picker_initial_thinking"] == "medium"


@pytest.mark.django_db
def test_hero_picker_seeds_default_agent_model_from_site_settings(member_client, enabled_provider, monkeypatch):
    """The hero picker pre-selects the system default — Alpine reads
    ``defaultAgentModel`` and seeds ``selectedProvider`` / ``modelName`` from it
    when nothing is stored. The wiring is what makes the "Auto row removal"
    change non-broken; without it the picker would render unselected and submit
    an empty spec."""
    from core.site_settings import site_settings

    monkeypatch.setattr(site_settings, "agent_model_name", "openrouter:anthropic/claude-sonnet-4.6")
    resp = member_client.get(reverse("chat_new"))
    assert resp.status_code == 200
    assert resp.context["agent_picker_default_model"] == "openrouter:anthropic/claude-sonnet-4.6"


@pytest.mark.django_db
def test_hero_picker_omits_default_when_provider_disabled(member_client, enabled_provider, monkeypatch):
    """If the system default points at a disabled provider, the seed is dropped
    so the picker falls back to the unselected ``Auto`` pill — see the matching
    unit test in ``test_picker_context``."""
    from core.site_settings import site_settings

    # Force ``anthropic`` into a disabled state — seed migrations leave it
    # enabled, so the default would otherwise be accepted.
    Provider.objects.filter(slug="anthropic").delete()
    Provider.objects.create(
        slug="anthropic",
        display_name="Anthropic",
        provider_type=ProviderType.ANTHROPIC,
        api_key="sk-x",
        is_enabled=False,
    )
    Provider.invalidate_cache()
    monkeypatch.setattr(site_settings, "agent_model_name", "anthropic:claude-sonnet-4.6")
    resp = member_client.get(reverse("chat_new"))
    assert resp.status_code == 200
    assert resp.context["agent_picker_default_model"] == ""


@pytest.mark.django_db
def test_thread_without_pinned_override_renders_auto_label(member_client, member_user, enabled_provider):
    """A thread that was created without an override shows ``Auto`` in the locked pill."""
    thread = ChatThread.objects.create(thread_id="t-auto", user=member_user, repo_id="a/b", ref="main")
    tup = MagicMock(checkpoint={"channel_values": {"messages": []}})
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.views.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))

    assert resp.status_code == 200
    assert resp.context["agent_picker_initial_model"] == ""
    assert resp.context["agent_picker_initial_thinking"] == ""
    # ``initial_agent_model|default:"Auto"`` resolves to ``Auto`` in the locked branch.
    assert "Auto" in resp.content.decode()
