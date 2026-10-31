from unittest.mock import AsyncMock, Mock, patch

import pytest
from sessions.models import InboxEvent
from webhooks.dispatch import Author, Mention
from webhooks.github.callbacks import (
    IssueCallback,
    IssueCommentCallback,
    PullRequestCallback,
    PullRequestReviewCommentCallback,
    PushCallback,
)
from webhooks.github.models import Comment, Issue, Label, PullRequest, Pusher, Ref, Repository, ReviewComment, User

from codebase.base import GitPlatform
from codebase.repo_config import RepositoryConfig
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL
from tests.unit_tests.sessions.conftest import amake_owned_merge_request


@pytest.fixture
def mock_repo_client():
    """Mock RepoClient instance."""
    client = Mock()
    client.has_issue_reaction.return_value = False
    return client


@pytest.fixture
def mock_repo_config():
    """Mock RepositoryConfig instance."""
    config = RepositoryConfig()
    config.issue_addressing.enabled = True
    return config


@pytest.fixture
def monkeypatch_dependencies(monkeypatch, mock_repo_client, mock_repo_config):
    """Monkeypatch RepoClient and RepositoryConfig for testing."""
    monkeypatch.setattr("webhooks.github.callbacks.RepoClient.create_instance", lambda: mock_repo_client)
    monkeypatch.setattr(
        "webhooks.github.callbacks.RepositoryConfig.get_config", lambda *args, **kwargs: mock_repo_config
    )


def create_issue_callback(
    action: str,
    issue_labels: list[Label],
    issue_state: str = "open",
    label: Label | None = None,
    sender_username: str = "testuser",
) -> IssueCallback:
    """Helper to create an IssueCallback instance."""
    return IssueCallback(
        action=action,
        repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
        issue=Issue(id=100, number=42, title="Test Issue", state=issue_state, labels=issue_labels),
        label=label,
        sender=User(**{"id": 10, "login": sender_username}),
    )


class TestIssueCallback:
    """Tests for GitHub IssueCallback."""

    def test_accept_callback_opened_with_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when issue is opened with a DAIV label."""
        callback = create_issue_callback(action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is True

    def test_accept_callback_labeled_with_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when a DAIV label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[Label(id=1, name=BOT_LABEL)], label=Label(id=1, name="daiv")
        )
        assert callback.accept_callback() is True

    def test_accept_callback_labeled_with_daiv_auto_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when daiv-auto label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[Label(id=3, name=BOT_AUTO_LABEL)], label=Label(id=2, name="daiv-auto")
        )
        assert callback.accept_callback() is True

    def test_accept_callback_labeled_with_daiv_max_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when daiv-max label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[Label(id=4, name=BOT_MAX_LABEL)], label=Label(id=3, name="daiv-max")
        )
        assert callback.accept_callback() is True

    def test_reject_callback_labeled_with_non_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is rejected when a non-DAIV label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[Label(id=2, name="bug")], label=Label(id=4, name="bug")
        )
        assert callback.accept_callback() is False

    def test_reject_callback_labeled_without_label_field(self, monkeypatch_dependencies):
        """Test that callback is rejected when labeled action has no label field."""
        callback = create_issue_callback(action="labeled", issue_labels=[Label(id=1, name=BOT_LABEL)], label=None)
        assert callback.accept_callback() is False

    def test_reject_callback_when_already_reacted(self, monkeypatch_dependencies, mock_repo_client):
        """Test that callback is rejected when DAIV has already reacted to the issue."""
        mock_repo_client.has_issue_reaction.return_value = True

        callback = create_issue_callback(action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is False

    def test_reject_callback_closed_issue(self, monkeypatch_dependencies):
        """Test that callback is rejected for closed issues."""
        callback = create_issue_callback(
            action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)], issue_state="closed"
        )
        assert callback.accept_callback() is False

    def test_reject_callback_edited_action(self, monkeypatch_dependencies):
        """Test that callback is rejected for edited action."""
        callback = create_issue_callback(action="edited", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is False

    def test_reject_callback_issue_addressing_disabled(
        self, monkeypatch_dependencies, mock_repo_config, mock_repo_client
    ):
        """Test that callback is rejected when issue addressing is disabled."""
        mock_repo_config.issue_addressing.enabled = False

        callback = create_issue_callback(action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is False

    def test_accept_callback_reopened_with_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when issue is reopened with a DAIV label."""
        callback = create_issue_callback(action="reopened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is True

    def test_reject_callback_reopened_without_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is rejected when issue is reopened without a DAIV label."""
        callback = create_issue_callback(action="reopened", issue_labels=[Label(id=2, name="bug")])
        assert callback.accept_callback() is False

    def test_label_check_case_insensitive(self, monkeypatch_dependencies):
        """Test that label checking is case-insensitive."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[Label(id=1, name="DAIV")], label=Label(id=1, name="DAIV")
        )
        assert callback.accept_callback() is True

    def test_reject_callback_user_not_in_allowlist(self, monkeypatch_dependencies, mock_repo_config):
        """Test that callback is rejected when user is not in the allowed usernames list."""
        mock_repo_config.allowed_usernames = ("alice", "bob")

        callback = create_issue_callback(
            action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)], sender_username="mallory"
        )
        assert callback.accept_callback() is False

    def test_accept_callback_user_in_allowlist(self, monkeypatch_dependencies, mock_repo_config):
        """Test that callback is accepted when user is in the allowed usernames list."""
        mock_repo_config.allowed_usernames = ("alice", "bob")

        callback = create_issue_callback(
            action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)], sender_username="alice"
        )
        assert callback.accept_callback() is True

    def test_accept_callback_empty_allowlist(self, monkeypatch_dependencies, mock_repo_config):
        """Test that callback is accepted when allowlist is empty (all users allowed)."""
        mock_repo_config.allowed_usernames = ()

        callback = create_issue_callback(action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is True

    def test_allowlist_case_insensitive(self, monkeypatch_dependencies, mock_repo_config):
        """Test that allowlist check is case-insensitive."""
        mock_repo_config.allowed_usernames = ("Alice",)

        callback = create_issue_callback(
            action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)], sender_username="alice"
        )
        assert callback.accept_callback() is True


class TestIssueCommentCallbackAllowlist:
    """Tests for GitHub IssueCommentCallback allowlist."""

    def test_reject_comment_user_not_in_allowlist(self, monkeypatch_dependencies, mock_repo_config, mock_repo_client):
        """Test that comment callback is rejected when user is not in the allowed usernames list."""
        mock_repo_config.allowed_usernames = ("alice",)
        mock_repo_client.current_user = User(**{"id": 999, "login": "daiv-bot"})

        callback = IssueCommentCallback(
            action="created",
            repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
            issue=Issue(id=100, number=42, title="Test Issue", state="open", labels=[Label(id=1, name=BOT_LABEL)]),
            comment=Comment(id=200, body="@daiv-bot help", user=User(**{"id": 10, "login": "mallory"})),
        )
        assert callback.accept_callback() is False

    def test_accept_comment_user_in_allowlist(self, monkeypatch_dependencies, mock_repo_config, mock_repo_client):
        """Test that comment callback is accepted when user is in the allowed usernames list."""
        mock_repo_config.allowed_usernames = ("alice",)
        mock_repo_client.current_user = User(**{"id": 999, "login": "daiv-bot"})

        callback = IssueCommentCallback(
            action="created",
            repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
            issue=Issue(id=100, number=42, title="Test Issue", state="open", labels=[Label(id=1, name=BOT_LABEL)]),
            comment=Comment(id=200, body="@daiv-bot help", user=User(**{"id": 10, "login": "alice"})),
        )
        assert callback.accept_callback() is True


def create_pull_request_callback(
    action: str = "closed",
    merged: bool = True,
    labels: list[Label] | None = None,
    user_login: str = "developer",
    base_ref: str = "main",
) -> PullRequestCallback:
    """Helper to create a PullRequestCallback instance."""
    return PullRequestCallback(
        action=action,
        repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
        sender=User(**{"id": 3, "login": "sfr"}),
        pull_request=PullRequest(
            id=10,
            number=42,
            title="Some PR",
            state="closed" if merged else "open",
            merged=merged,
            merged_at="2026-04-01T10:00:00Z" if merged else None,
            head=Ref(ref="feat/something", sha="abc123"),
            base=Ref(ref=base_ref, sha="def456"),
            labels=labels or [],
            user=User(**{"id": 2, "login": user_login}),
        ),
    )


class TestPullRequestCallback:
    """Tests for GitHub PullRequestCallback."""

    @pytest.mark.parametrize(("action", "merged"), [("closed", True), ("closed", False), ("reopened", False)])
    def test_accept_a_close_or_a_reopen(self, action, merged):
        assert create_pull_request_callback(action=action, merged=merged).accept_callback() is True

    @pytest.mark.parametrize("action", ["opened", "synchronize"])
    def test_reject_the_rest(self, action):
        assert create_pull_request_callback(action=action, merged=False).accept_callback() is False

    async def test_a_merge_into_the_default_branch_records_metrics_and_tells_the_owner(self):
        callback = create_pull_request_callback()
        with (
            patch("codebase.tasks.record_merge_metrics_task") as mock_task,
            patch("webhooks.github.callbacks.aclose_ownership", new=AsyncMock()) as close,
        ):
            mock_task.aenqueue = AsyncMock()
            await callback.process_callback()

        mock_task.aenqueue.assert_called_once_with(
            repo_id="owner/repo",
            merge_request_iid=42,
            title="Some PR",
            source_branch="feat/something",
            target_branch="main",
            merged_at="2026-04-01T10:00:00Z",
            platform="github",
        )
        close.assert_awaited_once_with("owner/repo", 42, merged=True, by_username="sfr", target_branch="main")

    @pytest.mark.parametrize(("merged", "base_ref"), [(True, "develop"), (False, "main")], ids=["elsewhere", "closed"])
    async def test_anything_but_a_merge_into_the_default_branch_records_no_metrics(self, merged, base_ref):
        callback = create_pull_request_callback(merged=merged, base_ref=base_ref)
        with (
            patch("codebase.tasks.record_merge_metrics_task") as mock_task,
            patch("webhooks.github.callbacks.aclose_ownership", new=AsyncMock()) as close,
        ):
            mock_task.aenqueue = AsyncMock()
            await callback.process_callback()

        mock_task.aenqueue.assert_not_called()
        assert close.await_args.kwargs["merged"] is merged

    async def test_a_reopen_tells_the_owner(self):
        callback = create_pull_request_callback(action="reopened", merged=False)
        with patch("webhooks.github.callbacks.areopen_ownership", new=AsyncMock()) as reopen:
            await callback.process_callback()

        reopen.assert_awaited_once_with("owner/repo", 42, by_username="sfr")

    async def test_process_callback_coalesces_none_merged_at(self):
        callback = create_pull_request_callback()
        callback.pull_request.merged_at = None
        with (
            patch("codebase.tasks.record_merge_metrics_task") as mock_task,
            patch("webhooks.github.callbacks.aclose_ownership", new=AsyncMock()),
        ):
            mock_task.aenqueue = AsyncMock()
            await callback.process_callback()

        assert mock_task.aenqueue.call_args.kwargs["merged_at"] == ""


@pytest.mark.django_db(transaction=True)
class TestPullRequestStateOnAnOwnedPullRequest:
    @pytest.fixture(autouse=True)
    def _no_drain(self):
        with patch("sessions.inbox.post.drain_logged"):
            yield

    @pytest.mark.parametrize(
        ("merged", "kind", "state"), [(True, "mr_merged", "merged"), (False, "mr_closed", "closed")]
    )
    async def test_a_merge_or_close_is_remembered_and_stops_the_watch(self, merged, kind, state):
        ownership = await amake_owned_merge_request(repo_id="owner/repo", merge_request_iid=42)

        await create_pull_request_callback(merged=merged, base_ref="develop").process_callback()

        event = await InboxEvent.objects.aget(session_id=ownership.session_id)
        assert (event.kind, event.source_key) == (kind, f"mr-state:42:{state}")
        await ownership.arefresh_from_db()
        assert ownership.closed_at is not None

    async def test_an_unowned_pull_request_posts_nothing(self):
        await create_pull_request_callback(merged=False).process_callback()

        assert not await InboxEvent.objects.aexists()


def create_push_callback(*, ref: str = "refs/heads/feat/x", deleted: bool = False) -> PushCallback:
    return PushCallback(
        ref=ref,
        repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
        before="e96f1f77" * 5,
        after="0a1b2c3d" * 5,
        deleted=deleted,
        pusher=Pusher(name="sfr"),
        commits=[{"id": "a"}, {"id": "b"}],
    )


@pytest.mark.django_db(transaction=True)
class TestPushCallback:
    async def test_a_push_to_an_owned_branch_is_weighed_a_minute_later(self):
        await amake_owned_merge_request(repo_id="owner/repo", ref="feat/x")
        task = Mock(aenqueue=AsyncMock())

        with patch("sessions.tasks.classify_push_task") as classify:
            classify.using.return_value = task
            await create_push_callback().process_callback()

        task.aenqueue.assert_awaited_once_with(
            repo_id="owner/repo",
            branch="feat/x",
            before="e96f1f77" * 5,
            after="0a1b2c3d" * 5,
            pusher_username="sfr",
            commit_count=2,
        )

    @pytest.mark.parametrize("deleted", [False, True], ids=["unowned", "deleted"])
    async def test_an_unowned_or_deleted_branch_is_ignored(self, deleted):
        if deleted:
            await amake_owned_merge_request(repo_id="owner/repo", ref="feat/x")

        with patch("sessions.tasks.classify_push_task") as classify:
            await create_push_callback(deleted=deleted).process_callback()

        classify.using.assert_not_called()

    async def test_a_push_to_the_default_branch_only_invalidates_the_config_cache(self):
        with (
            patch("webhooks.github.callbacks.RepositoryConfig.invalidate_cache") as invalidate,
            patch("sessions.tasks.classify_push_task") as classify,
        ):
            await create_push_callback(ref="refs/heads/main").process_callback()

        invalidate.assert_called_once_with("owner/repo")
        classify.using.assert_not_called()


def create_issue_comment_callback(*, labels: list[str], pull_request: dict | None = None) -> IssueCommentCallback:
    number, title, comment_id = (99, "PR", 300) if pull_request else (42, "Bug", 200)
    return IssueCommentCallback(
        action="created",
        repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
        issue=Issue(
            id=100,
            number=number,
            title=title,
            state="open",
            labels=[Label(id=1, name=name) for name in labels],
            pull_request=pull_request,
        ),
        comment=Comment(id=comment_id, body="@daiv help", user=User(**{"id": 10, "login": "alice"})),
    )


@pytest.mark.parametrize(("label", "use_max"), [(BOT_MAX_LABEL, True), (BOT_LABEL, False)])
class TestProcessCallback:
    """What each accepted event hands dispatch: the entity, its max mode, who triggered it and the mention."""

    async def test_an_issue_label_dispatches_an_issue_run(
        self, monkeypatch_dependencies, mock_repo_client, label, use_max
    ):
        callback = create_issue_callback(action="opened", issue_labels=[Label(id=1, name=label)])

        with patch("webhooks.github.callbacks.dispatch_issue_run", new=AsyncMock()) as dispatch:
            await callback.process_callback()

        dispatch.assert_awaited_once_with(
            client=mock_repo_client,
            repo_id="owner/repo",
            issue_iid=42,
            title="Test Issue",
            use_max=use_max,
            author=Author(provider=GitPlatform.GITHUB, uid=10, username="testuser"),
        )

    async def test_an_issue_comment_dispatches_an_issue_run_for_it(
        self, monkeypatch_dependencies, mock_repo_client, label, use_max
    ):
        callback = create_issue_comment_callback(labels=[label])

        with (
            patch("webhooks.github.callbacks.note_mentions_daiv", return_value=True),
            patch("webhooks.github.callbacks.dispatch_issue_run", new=AsyncMock()) as dispatch,
        ):
            await callback.process_callback()

        dispatch.assert_awaited_once_with(
            client=mock_repo_client,
            repo_id="owner/repo",
            issue_iid=42,
            title="Bug",
            use_max=use_max,
            author=Author(provider=GitPlatform.GITHUB, uid=10, username="alice"),
            mention=Mention(note_id=200, comment_id="200"),
        )

    async def test_a_pull_request_comment_dispatches_a_comment_run_leaving_the_branch_to_fetch(
        self, monkeypatch_dependencies, mock_repo_client, mock_repo_config, label, use_max
    ):
        mock_repo_config.pull_request_assistant.enabled = True
        callback = create_issue_comment_callback(labels=[label], pull_request={"url": "https://example/pr/99"})

        with (
            patch("webhooks.github.callbacks.note_mentions_daiv", return_value=True),
            patch("webhooks.github.callbacks.dispatch_mr_comment_run", new=AsyncMock()) as dispatch,
        ):
            await callback.process_callback()

        dispatch.assert_awaited_once_with(
            client=mock_repo_client,
            repo_id="owner/repo",
            merge_request_iid=99,
            ref=None,
            title="PR",
            use_max=use_max,
            author=Author(provider=GitPlatform.GITHUB, uid=10, username="alice"),
            mention=Mention(note_id=300, comment_id="300", body="@daiv help"),
        )


def create_review_comment_callback(
    body: str = "@daiv why this line?", *, action: str = "created", in_reply_to_id: int | None = None
) -> PullRequestReviewCommentCallback:
    return PullRequestReviewCommentCallback(
        action=action,
        repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
        pull_request=PullRequest(
            id=10,
            number=42,
            title="Some PR",
            state="open",
            head=Ref(ref="feat/x", sha="abc123"),
            base=Ref(ref="main", sha="def456"),
        ),
        comment=ReviewComment(
            id=901,
            body=body,
            user=User(**{"id": 10, "login": "alice"}),
            html_url="https://github.com/owner/repo/pull/42#discussion_r901",
            in_reply_to_id=in_reply_to_id,
        ),
    )


@pytest.mark.django_db(transaction=True)
class TestReviewCommentsOnAnOwnedPullRequest:
    @pytest.fixture(autouse=True)
    def inbox(self, monkeypatch_dependencies, mock_repo_client, mock_repo_config):
        mock_repo_config.pull_request_assistant.enabled = True
        mock_repo_client.current_user = User(**{"id": 1, "login": "daiv"})
        mock_repo_client.git_platform = GitPlatform.GITHUB
        with (
            patch("sessions.inbox.post.drain_logged"),
            patch("webhooks.dispatch.resolve_user", new=AsyncMock(return_value=None)),
        ):
            yield

    async def test_a_mention_reaches_the_owning_session_and_answers_in_its_thread(self, mock_repo_client):
        ownership = await amake_owned_merge_request(repo_id="owner/repo", merge_request_iid=42)

        await create_review_comment_callback(in_reply_to_id=900).process_callback()

        event = await InboxEvent.objects.aget(session_id=ownership.session_id)
        assert (event.kind, event.wake, event.source_key) == ("mention", True, "review-comment:901")
        assert (event.payload["discussion_id"], event.payload["is_diff"]) == ("900", True)
        mock_repo_client.create_merge_request_note_emoji.assert_called_once()

    async def test_a_comment_without_a_mention_is_remembered(self):
        ownership = await amake_owned_merge_request(repo_id="owner/repo", merge_request_iid=42)

        await create_review_comment_callback("Nit: rename this").process_callback()

        event = await InboxEvent.objects.aget(session_id=ownership.session_id)
        assert (event.kind, event.wake, event.source_key) == ("comment", False, "review-comment:901")

    @pytest.mark.parametrize("body", ["@daiv why this line?", "Nit: rename this"])
    async def test_an_unowned_pull_request_gets_nothing(self, mock_repo_client, body):
        with patch("webhooks.dispatch.address_mr_comments_task") as task:
            await create_review_comment_callback(body).process_callback()

        task.get_or_enqueue.assert_not_called()
        assert not await InboxEvent.objects.aexists()
        mock_repo_client.create_merge_request_note_emoji.assert_not_called()


class TestReviewCommentAcceptance:
    @pytest.fixture(autouse=True)
    def enabled(self, monkeypatch_dependencies, mock_repo_client, mock_repo_config):
        mock_repo_config.pull_request_assistant.enabled = True
        mock_repo_client.current_user = User(**{"id": 1, "login": "daiv"})

    def test_a_new_or_edited_comment_is_accepted(self):
        assert create_review_comment_callback().accept_callback() is True
        assert create_review_comment_callback(action="edited").accept_callback() is True

    def test_a_deleted_comment_is_rejected(self):
        assert create_review_comment_callback(action="deleted").accept_callback() is False

    def test_an_author_outside_the_allowlist_is_rejected(self, mock_repo_config):
        mock_repo_config.allowed_usernames = ["someone-else"]

        assert create_review_comment_callback().accept_callback() is False


@pytest.mark.django_db(transaction=True)
async def test_a_pull_request_comment_without_a_mention_on_an_owned_pull_request_is_remembered(
    monkeypatch_dependencies, mock_repo_client, mock_repo_config
):
    mock_repo_config.pull_request_assistant.enabled = True
    mock_repo_client.current_user = User(**{"id": 1, "login": "daiv"})
    mock_repo_client.git_platform = GitPlatform.GITHUB
    ownership = await amake_owned_merge_request(repo_id="owner/repo", merge_request_iid=99)
    callback = create_issue_comment_callback(labels=[], pull_request={"url": "https://example/pr/99"})
    callback.comment.body = "Thanks, looks fine"

    with (
        patch("sessions.inbox.post.drain_logged"),
        patch("webhooks.dispatch.resolve_user", new=AsyncMock(return_value=None)),
    ):
        assert callback.accept_callback() is True
        await callback.process_callback()

    event = await InboxEvent.objects.aget(session_id=ownership.session_id)
    assert (event.kind, event.source_key, event.payload["is_diff"]) == ("comment", "note:300", False)
