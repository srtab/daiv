from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sessions.models import Session, SessionOrigin

from accounts.models import User
from chat.api.threads import ChatSessionService, _extract_first_user_message
from core.models import Provider, ProviderType


@pytest.fixture
def openrouter_provider(db):
    Provider.objects.filter(slug="openrouter").delete()
    return Provider.objects.create(
        slug="openrouter", provider_type=ProviderType.OPENROUTER, api_key="sk-test", is_enabled=True
    )


def _fake_input(messages, *, role="user"):
    return SimpleNamespace(messages=[SimpleNamespace(role=role, content=c) for c in messages])


def test_extract_first_user_message_empty_returns_empty_string():
    assert _extract_first_user_message(_fake_input([])) == ""


def test_extract_first_user_message_skips_non_string_content():
    # AG-UI supports list-of-blocks content for multimodal messages; they shouldn't be used
    # as a title. We fall through to the next string-typed message.
    payload = _fake_input([[{"type": "text", "text": "hi"}], "fallback title"])
    assert _extract_first_user_message(payload) == "fallback title"


def test_extract_first_user_message_skips_whitespace_only():
    assert _extract_first_user_message(_fake_input(["   \n\t", "actual content"])) == "actual content"


def test_extract_first_user_message_returns_first_non_empty_string():
    assert _extract_first_user_message(_fake_input(["first", "second"])) == "first"


def test_extract_first_user_message_skips_non_user_roles():
    # Title should be derived from a human/user message, never from an assistant
    # bootstrap message that happened to land in input_data.messages first.
    msgs = SimpleNamespace(
        messages=[
            SimpleNamespace(role="assistant", content="Hi! How can I help?"),
            SimpleNamespace(role="user", content="actual ask"),
        ]
    )
    assert _extract_first_user_message(msgs) == "actual ask"


@pytest.mark.django_db(transaction=True)
async def test_persist_ref_updates_when_branch_changed():
    user = await User.objects.acreate_user(username="u-ref-1", email="ref1@x.com", password="x")  # noqa: S106
    await Session.objects.acreate(
        thread_id="t-ref-1", origin=SessionOrigin.CHAT, user=user, repo_id="a/b", ref="feature-x"
    )

    await ChatSessionService.persist_ref("t-ref-1", "feature-x", SimpleNamespace(source_branch="feature-y"))

    refreshed = await Session.objects.aget(thread_id="t-ref-1")
    assert refreshed.ref == "feature-y"
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_persist_ref_noop_when_branch_unchanged():
    with patch("chat.api.threads.Session.objects.filter") as filter_mock:
        await ChatSessionService.persist_ref("t-ref-2", "feature-x", SimpleNamespace(source_branch="feature-x"))

    filter_mock.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_persist_ref_noop_when_no_mr_captured():
    with patch("chat.api.threads.Session.objects.filter") as filter_mock:
        await ChatSessionService.persist_ref("t-ref-3", "feature-x", None)

    filter_mock.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_get_or_create_creates_chat_origin_session():
    user = await User.objects.acreate_user(username="u-create-1", email="create1@x.com", password="x")  # noqa: S106
    input_data = _fake_input(["hello"])

    session, created = await ChatSessionService.get_or_create_for_user(
        user=user, thread_id="t-create-1", repo_id="acme/x", ref="main", input_data=input_data
    )

    assert created is True
    assert session.origin == SessionOrigin.CHAT
    assert session.user_id == user.id
    assert session.repo_id == "acme/x"
    assert session.ref == "main"
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_override_pinned_on_session_creation(openrouter_provider):
    user = await User.objects.acreate_user(username="u-ov-1", email="ov1@x.com", password="x")  # noqa: S106
    input_data = _fake_input(["hello"])

    session, created = await ChatSessionService.get_or_create_for_user(
        user=user,
        thread_id="t-ov-1",
        repo_id="acme/x",
        ref="main",
        input_data=input_data,
        agent_model="openrouter:anthropic/claude-haiku-4.5",
        agent_thinking_level="low",
    )

    assert created is True
    assert session.agent_model == "openrouter:anthropic/claude-haiku-4.5"
    assert session.agent_thinking_level == "low"
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_override_ignored_on_existing_session(openrouter_provider):
    # First turn pins the override; the second turn supplies different values
    # but ``aget_or_create`` ignores defaults on hit, so the pinned values stand.
    user = await User.objects.acreate_user(username="u-ov-2", email="ov2@x.com", password="x")  # noqa: S106
    input_data = _fake_input(["hello"])

    _, first_created = await ChatSessionService.get_or_create_for_user(
        user=user,
        thread_id="t-ov-2",
        repo_id="acme/x",
        ref="main",
        input_data=input_data,
        agent_model="openrouter:anthropic/claude-haiku-4.5",
        agent_thinking_level="low",
    )
    session, created = await ChatSessionService.get_or_create_for_user(
        user=user,
        thread_id="t-ov-2",
        repo_id="acme/x",
        ref="main",
        input_data=input_data,
        agent_model="openrouter:anthropic/claude-opus-4.6",
        agent_thinking_level="high",
    )

    assert first_created is True
    assert created is False
    assert session.agent_model == "openrouter:anthropic/claude-haiku-4.5"
    assert session.agent_thinking_level == "low"
    await user.adelete()
