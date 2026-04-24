from unittest.mock import AsyncMock, MagicMock, patch

from django.urls import reverse

import pytest
from activity.models import Activity, TriggerType

from accounts.models import Role, User
from chat.models import ChatThread


@pytest.fixture
def other_user(db):
    return User.objects.create_user(username="other", email="other@test.com", password="x", role=Role.MEMBER)  # noqa: S106


@pytest.mark.django_db
def test_list_view_requires_login(client):
    resp = client.get(reverse("chat_list"))
    assert resp.status_code == 302
    assert "/accounts/login" in resp["Location"] or "login" in resp["Location"].lower()


@pytest.mark.django_db
def test_list_view_only_shows_users_threads(member_client, member_user, other_user):
    mine = ChatThread.objects.create(thread_id="t-mine", user=member_user, repo_id="a/b", ref="main")
    ChatThread.objects.create(thread_id="t-theirs", user=other_user, repo_id="a/b", ref="main")
    resp = member_client.get(reverse("chat_list"))
    assert resp.status_code == 200
    threads = list(resp.context["threads"])
    assert [t.thread_id for t in threads] == [mine.thread_id]


@pytest.mark.django_db
def test_detail_view_404s_for_other_user_thread(member_client, other_user):
    thread = ChatThread.objects.create(thread_id="t-other", user=other_user, repo_id="a/b", ref="main")
    resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_detail_view_with_live_checkpoint_renders_transcript(member_client, member_user):
    from langchain_core.messages import AIMessage

    thread = ChatThread.objects.create(thread_id="t-live", user=member_user, repo_id="a/b", ref="main")
    msg = AIMessage(content="hello from agent", id="m-1")
    tup = MagicMock(checkpoint={"channel_values": {"messages": [msg]}})
    with patch("chat.views.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is False
    turns = resp.context["turns"]
    assert len(turns) == 1
    assert turns[0]["role"] == "assistant"
    assert turns[0]["segments"] == [{"type": "text", "content": "hello from agent"}]


@pytest.mark.django_db
def test_detail_view_with_missing_checkpoint_flags_expired(member_client, member_user):
    thread = ChatThread.objects.create(thread_id="t-gone", user=member_user, repo_id="a/b", ref="main")
    with patch("chat.views.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))

    assert resp.status_code == 200
    assert resp.context["expired"] is True


@pytest.mark.django_db
def test_detail_view_empty_state_renders_new_page(member_client):
    resp = member_client.get(reverse("chat_new"))
    assert resp.status_code == 200
    assert resp.context["thread"] is None
    assert resp.context["expired"] is False


@pytest.mark.django_db
def test_from_activity_404_for_other_users_activity(member_client, other_user):
    activity = Activity.objects.create(
        trigger_type=TriggerType.UI_JOB, repo_id="a/b", ref="main", prompt="x", thread_id="t-x", user=other_user
    )
    resp = member_client.post(reverse("chat_from_activity", kwargs={"activity_id": activity.id}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_from_activity_404_when_activity_has_no_thread_id(member_client, member_user):
    activity = Activity.objects.create(
        trigger_type=TriggerType.UI_JOB, repo_id="a/b", ref="main", prompt="x", user=member_user
    )
    resp = member_client.post(reverse("chat_from_activity", kwargs={"activity_id": activity.id}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_from_activity_410_when_checkpoint_missing(member_client, member_user):
    activity = Activity.objects.create(
        trigger_type=TriggerType.UI_JOB, repo_id="a/b", ref="main", prompt="x", thread_id="t-gone", user=member_user
    )
    with patch("chat.views.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=None)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.post(reverse("chat_from_activity", kwargs={"activity_id": activity.id}))
    assert resp.status_code == 410


@pytest.mark.django_db
def test_from_activity_creates_thread_and_redirects(member_client, member_user):
    activity = Activity.objects.create(
        trigger_type=TriggerType.UI_JOB,
        repo_id="a/b",
        ref="main",
        prompt="hello there",
        thread_id="t-alive",
        user=member_user,
    )
    tup = MagicMock(checkpoint={"channel_values": {"messages": []}})
    with patch("chat.views.open_checkpointer") as cp_ctx:
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.post(reverse("chat_from_activity", kwargs={"activity_id": activity.id}))
    assert resp.status_code == 302
    assert ChatThread.objects.filter(thread_id="t-alive", user=member_user).exists()
    assert resp["Location"] == reverse("chat_detail", kwargs={"thread_id": "t-alive"})
