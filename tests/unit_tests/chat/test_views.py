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
def test_list_view_filters_by_title_q(member_client, member_user):
    ChatThread.objects.create(thread_id="t-1", user=member_user, repo_id="a/b", title="Auth refactor")
    ChatThread.objects.create(thread_id="t-2", user=member_user, repo_id="a/b", title="Database migration")

    resp = member_client.get(reverse("chat_list"), {"q": "auth"})

    assert resp.status_code == 200
    threads = list(resp.context["threads"])
    assert [t.thread_id for t in threads] == ["t-1"]


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="x",  # noqa: S106
        role=Role.ADMIN,
    )


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.mark.django_db
def test_list_view_admin_with_all_param_sees_all_threads(admin_client, admin_user, other_user):
    ChatThread.objects.create(thread_id="t-mine", user=admin_user, repo_id="a/b")
    ChatThread.objects.create(thread_id="t-theirs", user=other_user, repo_id="a/b")

    resp = admin_client.get(reverse("chat_list"), {"all": "1"})

    ids = {t.thread_id for t in resp.context["threads"]}
    assert ids == {"t-mine", "t-theirs"}


@pytest.mark.django_db
def test_list_view_admin_without_all_param_sees_only_own(admin_client, admin_user, other_user):
    ChatThread.objects.create(thread_id="t-mine", user=admin_user, repo_id="a/b")
    ChatThread.objects.create(thread_id="t-theirs", user=other_user, repo_id="a/b")

    resp = admin_client.get(reverse("chat_list"))

    ids = {t.thread_id for t in resp.context["threads"]}
    assert ids == {"t-mine"}


@pytest.mark.django_db
def test_list_view_member_with_all_param_still_only_sees_own(member_client, member_user, other_user):
    ChatThread.objects.create(thread_id="t-mine", user=member_user, repo_id="a/b")
    ChatThread.objects.create(thread_id="t-theirs", user=other_user, repo_id="a/b")

    resp = member_client.get(reverse("chat_list"), {"all": "1"})

    ids = {t.thread_id for t in resp.context["threads"]}
    assert ids == {"t-mine"}


@pytest.mark.django_db
def test_list_view_filters_by_repo_id(member_client, member_user):
    ChatThread.objects.create(thread_id="t-a", user=member_user, repo_id="a/b", title="X")
    ChatThread.objects.create(thread_id="t-b", user=member_user, repo_id="c/d", title="Y")

    resp = member_client.get(reverse("chat_list"), {"repo_id": "c/d"})

    assert [t.thread_id for t in resp.context["threads"]] == ["t-b"]


@pytest.mark.django_db
def test_list_view_filters_by_status_active(member_client, member_user):
    ChatThread.objects.create(thread_id="t-active", user=member_user, repo_id="a/b", title="X", active_run_id="r1")
    ChatThread.objects.create(thread_id="t-idle", user=member_user, repo_id="a/b", title="Y")

    resp = member_client.get(reverse("chat_list"), {"status": "active"})

    assert [t.thread_id for t in resp.context["threads"]] == ["t-active"]


@pytest.mark.django_db
def test_list_view_filters_by_status_idle(member_client, member_user):
    ChatThread.objects.create(thread_id="t-active", user=member_user, repo_id="a/b", title="X", active_run_id="r1")
    ChatThread.objects.create(thread_id="t-idle", user=member_user, repo_id="a/b", title="Y")

    resp = member_client.get(reverse("chat_list"), {"status": "idle"})

    assert [t.thread_id for t in resp.context["threads"]] == ["t-idle"]


@pytest.mark.django_db
def test_list_view_rows_fragment_returns_rows_only(member_client, member_user):
    ChatThread.objects.create(thread_id="t-1", user=member_user, repo_id="a/b", title="X")

    resp = member_client.get(reverse("chat_list"), {"fragment": "rows"})

    assert resp.status_code == 200
    template_names = {t.name for t in resp.templates if t.name}
    assert "chat/_thread_rows.html" in template_names
    assert "chat/chat_list.html" not in template_names


@pytest.mark.django_db
def test_list_view_full_render_includes_workspace_shell(member_client, member_user):
    resp = member_client.get(reverse("chat_list"))

    assert resp.status_code == 200
    template_names = {t.name for t in resp.templates if t.name}
    assert "_workspace.html" in template_names
    assert "chat/chat_list.html" in template_names


@pytest.mark.django_db
def test_list_view_rows_fragment_paginates_with_sentinel(member_client, member_user):
    for i in range(30):
        ChatThread.objects.create(thread_id=f"t-{i:02d}", user=member_user, repo_id="a/b", title=f"T{i}")

    page1 = member_client.get(reverse("chat_list"))
    assert b'hx-trigger="revealed once"' in page1.content

    page2 = member_client.get(reverse("chat_list"), {"page": 2, "fragment": "rows"})
    assert page2.status_code == 200
    # Only 30 - 25 = 5 rows on page 2; no sentinel because there's no page 3.
    assert b'hx-trigger="revealed once"' not in page2.content


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
def test_detail_view_exposes_usage_summary_for_messages_with_metadata(member_client, member_user):
    from langchain_core.messages import AIMessage

    thread = ChatThread.objects.create(thread_id="t-usage", user=member_user, repo_id="a/b", ref="main")
    msg = AIMessage(content="x", id="m-1")
    msg.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    msg.response_metadata = {"model_name": "anthropic/claude-sonnet-4.6"}

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
    summary = resp.context["usage_summary"]
    assert summary["total_tokens"] == 15
    assert summary["input_tokens"] == 10
    assert summary["output_tokens"] == 5


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
    """Platform hiccups must not break the page — but only platform-typed
    exceptions are swallowed. Programming bugs propagate.
    """
    import httpx

    thread = ChatThread.objects.create(thread_id="t-err", user=member_user, repo_id="a/b", ref="feature-z")
    tup = MagicMock(checkpoint={"channel_values": {"messages": []}})
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.repo_state.RepositoryConfig.get_config", side_effect=httpx.ConnectError("platform unreachable")),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))

    assert resp.status_code == 200
    assert resp.context["merge_request"] is None


@pytest.mark.django_db
def test_detail_view_propagates_unexpected_errors_in_mr_lookup(member_client, member_user):
    """Bugs (KeyError/AttributeError/TypeError) must NOT be silently swallowed
    by the soft-fallback path — they would mask real failures behind a
    plausible-looking "no MR" UI.
    """
    thread = ChatThread.objects.create(thread_id="t-bug", user=member_user, repo_id="a/b", ref="feature-z")
    tup = MagicMock(checkpoint={"channel_values": {"messages": []}})
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.repo_state.RepositoryConfig.get_config", side_effect=KeyError("config missing")),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=tup)
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        with pytest.raises(KeyError):
            member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))


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
def test_list_view_htmx_filter_request_returns_thread_rows(member_client, member_user):
    ChatThread.objects.create(thread_id="t-1", user=member_user, repo_id="a/b", title="X")
    resp = member_client.get(reverse("chat_list"), {"q": "X"}, headers={"HX-Request": "true"})
    template_names = {t.name for t in resp.templates if t.name}
    assert "chat/_thread_list.html" in template_names
    assert "chat/chat_list.html" not in template_names


@pytest.mark.django_db
def test_detail_view_htmx_returns_partial_only(member_client, member_user):
    thread = ChatThread.objects.create(thread_id="t-htmx", user=member_user, repo_id="a/b", ref="main")
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.views.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=MagicMock(checkpoint={"channel_values": {"messages": []}}))
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(
            reverse("chat_detail", kwargs={"thread_id": thread.thread_id}), headers={"HX-Request": "true"}
        )

    template_names = {t.name for t in resp.templates if t.name}
    assert "chat/_detail.html" in template_names
    assert "_workspace.html" not in template_names


@pytest.mark.django_db
def test_detail_view_primes_initial_pane_to_detail(member_client, member_user):
    thread = ChatThread.objects.create(thread_id="t-pane", user=member_user, repo_id="a/b", ref="main")
    with (
        patch("chat.views.open_checkpointer") as cp_ctx,
        patch("chat.views.aget_existing_mr_payload", AsyncMock(return_value=None)),
    ):
        saver = MagicMock()
        saver.aget_tuple = AsyncMock(return_value=MagicMock(checkpoint={"channel_values": {"messages": []}}))
        cp_ctx.return_value.__aenter__ = AsyncMock(return_value=saver)
        cp_ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        resp = member_client.get(reverse("chat_detail", kwargs={"thread_id": thread.thread_id}))

    assert resp.context["initial_pane"] == "detail"


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
