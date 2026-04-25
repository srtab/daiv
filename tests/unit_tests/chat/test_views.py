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
    assert resp.context["expired"] is False
    turns = resp.context["turns"]
    assert len(turns) == 1
    assert turns[0]["role"] == "assistant"
    assert turns[0]["segments"] == [{"type": "text", "content": "hello from agent"}]


@pytest.mark.django_db
def test_detail_view_with_missing_checkpoint_flags_expired(member_client, member_user):
    thread = ChatThread.objects.create(thread_id="t-gone", user=member_user, repo_id="a/b", ref="main")
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.views.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
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
def test_detail_view_surfaces_existing_mr_when_checkpoint_has_none(member_client, member_user):
    """Composer should show an MR pill when one already exists for the chat's branch."""
    from codebase.base import MergeRequest
    from codebase.base import User as CBUser

    thread = ChatThread.objects.create(thread_id="t-mr", user=member_user, repo_id="a/b", ref="feature-x")
    tup = MagicMock(checkpoint={"channel_values": {"messages": []}})
    existing_mr = MergeRequest(
        repo_id="a/b",
        merge_request_id=42,
        source_branch="feature-x",
        target_branch="main",
        title="Add feature X",
        description="",
        labels=[],
        web_url="https://gitlab.example/a/b/-/merge_requests/42",
        sha="deadbeef",
        author=CBUser(id=1, username="u", name="U"),
        draft=True,
    )
    repo_client = MagicMock()
    repo_client.get_merge_request_by_branches.return_value = existing_mr
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.repo_state.RepoClient.create_instance", return_value=repo_client),
        patch("chat.repo_state.RepositoryConfig.get_config", return_value=MagicMock(default_branch="main")),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))

    assert resp.status_code == 200
    mr = resp.context["merge_request"]
    assert mr is not None
    assert mr["id"] == 42
    assert mr["url"] == "https://gitlab.example/a/b/-/merge_requests/42"
    assert mr["draft"] is True
    repo_client.get_merge_request_by_branches.assert_called_once_with("a/b", "feature-x", "main")


@pytest.mark.django_db
def test_detail_view_skips_mr_lookup_when_checkpoint_already_has_one(member_client, member_user):
    """When LangGraph state already carries an MR, don't hit the platform."""
    thread = ChatThread.objects.create(thread_id="t-cached", user=member_user, repo_id="a/b", ref="feature-y")
    stored_mr = {
        "merge_request_id": 7,
        "web_url": "https://example/7",
        "title": "Stored",
        "draft": False,
        "source_branch": "feature-y",
        "target_branch": "main",
    }
    tup = MagicMock(checkpoint={"channel_values": {"messages": [], "merge_request": stored_mr}})
    repo_client = MagicMock()
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.repo_state.RepoClient.create_instance", return_value=repo_client) as factory,
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))

    assert resp.status_code == 200
    assert resp.context["merge_request"]["id"] == 7
    factory.assert_not_called()


@pytest.mark.django_db
def test_detail_view_swallows_platform_errors_in_mr_lookup(member_client, member_user):
    """Platform hiccups must not break the page."""
    thread = ChatThread.objects.create(thread_id="t-err", user=member_user, repo_id="a/b", ref="feature-z")
    tup = MagicMock(checkpoint={"channel_values": {"messages": []}})
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.repo_state.RepositoryConfig.get_config", side_effect=RuntimeError("platform unreachable")),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))

    assert resp.status_code == 200
    assert resp.context["merge_request"] is None


@pytest.mark.django_db
def test_detail_view_skips_mr_lookup_when_branch_is_default(member_client, member_user):
    """No MR makes sense when source == target, so don't ask the platform."""
    thread = ChatThread.objects.create(thread_id="t-main", user=member_user, repo_id="a/b", ref="main")
    tup = MagicMock(checkpoint={"channel_values": {"messages": []}})
    repo_client = MagicMock()
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.repo_state.RepoClient.create_instance", return_value=repo_client) as factory,
        patch("chat.repo_state.RepositoryConfig.get_config", return_value=MagicMock(default_branch="main")),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))

    assert resp.status_code == 200
    assert resp.context["merge_request"] is None
    factory.assert_not_called()
    repo_client.get_merge_request_by_branches.assert_not_called()


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
