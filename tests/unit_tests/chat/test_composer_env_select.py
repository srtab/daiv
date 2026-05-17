"""Tests for the sandbox_environment dropdown on the chat composer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from django.urls import reverse

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from chat.models import ChatThread


@pytest.mark.django_db
def test_composer_context_lists_user_and_global_envs(member_client, member_user):
    """The chat detail view should expose ``sandbox_envs`` in the template context with
    the caller's USER envs plus all GLOBAL envs (and not other users' USER envs)."""
    user_env = SandboxEnvironment.objects.create(scope=Scope.USER, user=member_user, name="my-env", base_image="x")
    extra_global = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="ExtraGlobal", base_image="g")
    from accounts.models import User

    other = User.objects.create_user(username="other_c", email="other_c@e.com", password="x")  # noqa: S106
    SandboxEnvironment.objects.create(scope=Scope.USER, user=other, name="other-env", base_image="y")

    resp = member_client.get(reverse("chat_new"))
    assert resp.status_code == 200
    sandbox_envs = resp.context["sandbox_envs"]
    ids = {e.id for e in sandbox_envs}
    assert user_env.id in ids
    assert extra_global.id in ids
    # Other users' USER-scoped envs are not visible.
    assert not any(e.name == "other-env" for e in sandbox_envs)


@pytest.mark.django_db
def test_composer_renders_env_picker_with_envs(member_client, member_user):
    """The composer template renders the env-picker partial populated from context."""
    user_env = SandboxEnvironment.objects.create(scope=Scope.USER, user=member_user, name="zeta-env", base_image="x")

    thread = ChatThread.objects.create(thread_id="t-env", user=member_user, repo_id="a/b", ref="main")
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
    # Hidden input for form contract (still present even though chat uses @submit.prevent).
    assert 'name="sandbox_environment"' in body
    # Env-picker partial root present.
    assert "envPicker(" in body
    # USER env reached the partial's JSON payload (hyphens are escaped by |escapejs).
    assert "zeta\\u002Denv" in body
    # And it's in the context queryset for sanity.
    assert any(e.id == user_env.id for e in resp.context["sandbox_envs"])
