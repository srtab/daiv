import inspect
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.sites.models import Site

import pytest

from automation.agent.git_manager import RepoStatus
from automation.agent.publishers import GitChangePublisher, PublishOutcome
from codebase.base import GitPlatform, Issue, MergeRequest, MergeRequestDiffStats, User
from codebase.clients.base import GitAuthEnv
from codebase.exceptions import MergeRequestBranchNotVisibleError
from core.constants import BOT_AUTO_LABEL, BOT_NAME
from core.site_settings import site_settings

# The fake ``"diff"`` body has no hunks, so counting it yields zeros.
_LOCAL_STATS = MergeRequestDiffStats()


def _fake_git_manager(*, dirty: bool = True, diff: str = "diff", remote_branches=(), has_unpushed: bool = True) -> Mock:
    """A stand-in for the (sandbox/local) GitManager the publisher opens via open_git_manager.

    The publisher reads everything it needs from a single ``status_snapshot``; the mutation methods
    (``commit_all``/``push_head_to``) stay separate AsyncMocks.
    """
    gm = Mock()
    gm.status_snapshot = AsyncMock(
        return_value=RepoStatus(
            dirty=dirty, diff=diff, remote_branches=list(remote_branches), has_unpushed=has_unpushed
        )
    )
    gm.commit_all = AsyncMock()
    gm.push_head_to = AsyncMock(return_value="pushed")
    gm.unique_branch_name = Mock(side_effect=lambda name, existing: name)
    return gm


def _patch_open_git_manager(monkeypatch, gm: Mock) -> dict:
    """Swap in a fake manager and return a dict capturing the kwargs the publisher opened it with."""
    captured = {}

    @asynccontextmanager
    async def _fake_open(*, sandbox_backend, gitrepo, auth_env=None):  # noqa: ARG001
        captured["auth_env"] = auth_env
        yield gm

    monkeypatch.setattr("automation.agent.publishers.open_git_manager", _fake_open)
    return captured


def _make_merge_request(**overrides) -> MergeRequest:
    defaults = {
        "repo_id": "owner/repo",
        "merge_request_id": 42,
        "source_branch": "feature",
        "target_branch": "main",
        "title": "Test MR",
        "description": "Test description",
        "web_url": "https://example.com/owner/repo/-/merge_requests/42",
        "author": User(id=1, username="testuser"),
    }
    defaults.update(overrides)
    return MergeRequest(**defaults)


def _make_publisher(
    *,
    git_platform: GitPlatform = GitPlatform.GITLAB,
    context_file_name: str | None = "AGENTS.md",
    thread_id: str | None = None,
):
    ctx = Mock()
    ctx.repository.slug = "owner/repo"
    ctx.repository.html_url = "https://gitlab.com/owner/repo"
    ctx.repository.git_platform = git_platform
    ctx.config.context_file_name = context_file_name
    ctx.config.suggest_context_file = True
    ctx.config.session_link = True
    ctx.config.default_branch = "main"
    ctx.git_platform = git_platform

    if git_platform == GitPlatform.GITHUB:
        ctx.repository.html_url = "https://github.com/owner/repo"

    publisher = GitChangePublisher(ctx, thread_id=thread_id)
    publisher.client = Mock()
    publisher.client.is_branch_protected.return_value = False
    publisher.client.push_uses_ephemeral_token.return_value = False
    return publisher


def _metadata_stub():
    commit = Mock()
    commit.commit_message = "msg"
    pr = Mock()
    pr.branch = "feature"
    pr.title = "Title"
    pr.description = "Body"
    return {"commit_message": commit, "pr_metadata": pr}


def _make_sandbox_publisher(*, egress="default"):
    """A sandbox-mode publisher: bound backend mock (with an awaitable ``refresh_egress``) and a
    real turn-start egress config on ctx — what the pre-publish refresh reads. Pass ``egress=None``
    for a run without an egress proxy."""
    from core.sandbox.schemas import EgressConfigRequest

    publisher = _make_publisher()
    publisher.sandbox_backend = Mock()
    publisher.sandbox_backend.refresh_egress = AsyncMock()
    publisher.ctx.sandbox.egress = EgressConfigRequest() if egress == "default" else egress
    return publisher


class TestSuggestContextFile:
    async def test_posts_comment_when_file_missing(self):
        publisher = _make_publisher()
        publisher.client.get_repository_file.return_value = None
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_called_once_with("owner/repo", "AGENTS.md", ref="main")
        publisher.client.create_merge_request_comment.assert_called_once()
        comment_body = publisher.client.create_merge_request_comment.call_args[0][2]
        assert "AGENTS.md" in comment_body
        assert BOT_NAME in comment_body

    async def test_skips_when_file_exists(self):
        publisher = _make_publisher()
        publisher.client.get_repository_file.return_value = "# AGENTS.md content"
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_called_once()
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_skips_when_context_file_name_none(self):
        publisher = _make_publisher(context_file_name=None)
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_skips_when_context_file_name_empty(self):
        publisher = _make_publisher(context_file_name="")
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_skips_when_disabled_per_repo(self):
        publisher = _make_publisher()
        publisher.ctx.config.suggest_context_file = False
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_skips_when_globally_disabled(self, monkeypatch):
        from core.site_settings import site_settings

        monkeypatch.setattr(site_settings, "suggest_context_file_enabled", False)
        publisher = _make_publisher()
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_not_called()
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_does_not_raise_on_error(self):
        publisher = _make_publisher()
        publisher.client.get_repository_file.side_effect = Exception("API error")
        mr = _make_merge_request()

        # Should not raise
        await publisher._suggest_context_file(mr)

    async def test_custom_context_file_name(self):
        publisher = _make_publisher(context_file_name="CLAUDE.md")
        publisher.client.get_repository_file.return_value = None
        mr = _make_merge_request()

        await publisher._suggest_context_file(mr)

        publisher.client.get_repository_file.assert_called_once_with("owner/repo", "CLAUDE.md", ref="main")
        comment_body = publisher.client.create_merge_request_comment.call_args[0][2]
        assert "CLAUDE.md" in comment_body


def _publisher_no_issue(
    *, git_platform: GitPlatform = GitPlatform.GITLAB, thread_id: str | None = None
) -> GitChangePublisher:
    """A publisher rendering the description template on its own, with no issue to close."""
    publisher = _make_publisher(git_platform=git_platform, thread_id=thread_id)
    publisher.ctx.issue = None
    publisher.ctx.bot_username = "daiv"
    return publisher


class TestCreateMergeRequestDescription:
    """The new MR description back-links to the original protected MR for traceability."""

    async def test_includes_back_link_when_fallback_provided(self):
        publisher = _publisher_no_issue()
        original = _make_merge_request(
            source_branch="dev", merge_request_id=42, web_url="https://gitlab.com/owner/repo/-/merge_requests/42"
        )

        await publisher._create_merge_request("feature-fix", "Title", "Body", as_draft=False, fallback_from_mr=original)

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "dev" in description
        assert "https://gitlab.com/owner/repo/-/merge_requests/42" in description
        assert "!42" in description

    async def test_omits_back_link_when_no_fallback(self):
        publisher = _publisher_no_issue()

        await publisher._create_merge_request("feature", "Title", "Body", as_draft=False)

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "is protected on the remote" not in description

    async def test_back_link_uses_github_terminology(self):
        publisher = _publisher_no_issue(git_platform=GitPlatform.GITHUB)
        original = _make_merge_request(
            source_branch="main", merge_request_id=10, web_url="https://github.com/owner/repo/pull/10"
        )

        await publisher._create_merge_request("feature-fix", "Title", "Body", as_draft=False, fallback_from_mr=original)

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "#10" in description
        assert "!10" not in description

    async def test_fallback_mr_inherits_original_target(self):
        """A protected-branch fallback MR targets the original MR's target, not the default."""
        publisher = _publisher_no_issue()
        original = _make_merge_request(source_branch="dev", target_branch="release/x", merge_request_id=42)

        await publisher._create_merge_request("feature-fix", "Title", "Body", fallback_from_mr=original)

        assert publisher.client.update_or_create_merge_request.call_args.kwargs["target_branch"] == "release/x"

    async def test_new_mr_targets_default_without_fallback(self):
        publisher = _publisher_no_issue()

        await publisher._create_merge_request("feature", "Title", "Body")

        assert publisher.client.update_or_create_merge_request.call_args.kwargs["target_branch"] == "main"


def _disable_site_wide(publisher, monkeypatch) -> None:  # noqa: ARG001
    monkeypatch.setattr(site_settings, "session_link_enabled", False)


def _disable_per_repository(publisher, monkeypatch) -> None:  # noqa: ARG001
    publisher.ctx.config.session_link = False


class TestCreateMergeRequestSessionLink:
    """The MR description footer links back to the DAIV session that produced it."""

    @pytest.fixture
    def linked_description(self, monkeypatch):
        """Render a description with the link resolver stubbed to a known domain."""

        async def _render(thread_id: str = "abc123def") -> str:
            monkeypatch.setattr(
                "automation.agent.publishers.build_absolute_url", lambda path: f"https://daiv.test{path}"
            )
            publisher = _publisher_no_issue(thread_id=thread_id)
            await publisher._create_merge_request("feature", "Title", "Body")
            return publisher.client.update_or_create_merge_request.call_args.kwargs["description"]

        return _render

    async def test_includes_session_link_when_thread_id_set(self, linked_description):
        """The description points at the MR-scoped resolver, not one thread's transcript, so
        sessions that join the MR later are reachable from the same URL."""
        assert (
            "[view sessions](https://daiv.test/dashboard/sessions/abc123def/merge-request/)"
            in await linked_description()
        )

    async def test_link_shares_the_warning_blockquote(self, linked_description):
        """A blank line between the two would split the footer into two quote boxes."""
        lines = (await linked_description()).splitlines()
        warning_index = next(i for i, line in enumerate(lines) if "can make mistakes" in line)
        assert "view sessions" in lines[warning_index + 1]

    @pytest.mark.parametrize(
        ("thread_id", "disable"),
        [
            pytest.param(None, None, id="no-thread-id"),
            pytest.param("", None, id="empty-thread-id"),
            pytest.param("abc123def", _disable_site_wide, id="disabled-site-wide"),
            pytest.param("abc123def", _disable_per_repository, id="disabled-per-repository"),
        ],
    )
    async def test_omits_session_link_without_resolving_a_url(self, monkeypatch, thread_id, disable):
        """Every disabled path short-circuits before the Site lookup, not after."""
        build_absolute_url = Mock()
        monkeypatch.setattr("automation.agent.publishers.build_absolute_url", build_absolute_url)
        publisher = _publisher_no_issue(thread_id=thread_id)
        if disable is not None:
            disable(publisher, monkeypatch)

        await publisher._create_merge_request("feature", "Title", "Body")

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "view sessions" not in description
        build_absolute_url.assert_not_called()

    @pytest.mark.parametrize(
        ("thread_id", "url_builder"),
        [
            pytest.param("not a slug", None, id="unroutable-thread-id"),
            pytest.param("abc123def", Mock(side_effect=Site.DoesNotExist("no site")), id="missing-site-row"),
        ],
    )
    async def test_unresolvable_link_still_publishes(self, monkeypatch, thread_id, url_builder):
        """A link that cannot be built must not abort a publish whose branch is already pushed."""
        if url_builder is not None:
            monkeypatch.setattr("automation.agent.publishers.build_absolute_url", url_builder)
        publisher = _publisher_no_issue(thread_id=thread_id)

        await publisher._create_merge_request("feature", "Title", "Body")

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "view sessions" not in description
        assert "can make mistakes" in description


class TestCommitSessionTrailer:
    """Commits carry the producing session as a git trailer."""

    @pytest.fixture
    def trailered(self, monkeypatch):
        async def _build(message: str = "msg", thread_id: str = "abc123def"):
            monkeypatch.setattr(
                "automation.agent.publishers.build_absolute_url", lambda path: f"https://daiv.test{path}"
            )
            return await _publisher_no_issue(thread_id=thread_id)._with_session_trailer(message)

        return _build

    async def test_appends_trailer_referencing_the_session(self, trailered):
        assert await trailered() == "msg\n\nDAIV-Session: https://daiv.test/dashboard/sessions/abc123def/"

    async def test_trailer_is_its_own_paragraph(self, trailered):
        """A trailer sharing the previous paragraph is not parsed by `git interpret-trailers`."""
        body, _, trailer = (await trailered("subject\n\nbody paragraph")).rpartition("\n\n")
        assert body == "subject\n\nbody paragraph"
        assert trailer.startswith("DAIV-Session: ")

    async def test_collapses_trailing_whitespace_before_the_trailer(self, trailered):
        assert await trailered("msg\n\n") == "msg\n\nDAIV-Session: https://daiv.test/dashboard/sessions/abc123def/"

    @pytest.mark.parametrize(
        ("thread_id", "disable"),
        [
            pytest.param(None, None, id="no-thread-id"),
            pytest.param("abc123def", _disable_site_wide, id="disabled-site-wide"),
            pytest.param("abc123def", _disable_per_repository, id="disabled-per-repository"),
        ],
    )
    async def test_message_untouched_when_unavailable(self, monkeypatch, thread_id, disable):
        publisher = _publisher_no_issue(thread_id=thread_id)
        if disable is not None:
            disable(publisher, monkeypatch)

        assert await publisher._with_session_trailer("msg") == "msg"

    async def test_unresolvable_link_leaves_the_commit_message_intact(self, monkeypatch):
        """The commit must survive a misconfigured Sites row; only the trailer is lost."""
        monkeypatch.setattr(
            "automation.agent.publishers.build_absolute_url", Mock(side_effect=Site.DoesNotExist("no site"))
        )
        publisher = _publisher_no_issue(thread_id="abc123def")

        assert await publisher._with_session_trailer("msg") == "msg"


class TestCreateMergeRequestAssignee:
    """_create_merge_request assigns the MR to the issue assignee, falling back to the author."""

    def _issue(self, *, assignee, author):
        return Issue(iid=5, title="t", description="d", assignee=assignee, author=author)

    async def test_uses_issue_assignee_when_present_gitlab(self):
        publisher = _make_publisher(git_platform=GitPlatform.GITLAB)
        publisher.ctx.issue = self._issue(
            assignee=User(id=7, username="assignee"), author=User(id=9, username="author")
        )

        await publisher._create_merge_request("feature", "Title", "Body")

        assert publisher.client.update_or_create_merge_request.call_args.kwargs["assignee_id"] == 7

    async def test_falls_back_to_author_when_unassigned_gitlab(self):
        publisher = _make_publisher(git_platform=GitPlatform.GITLAB)
        publisher.ctx.issue = self._issue(assignee=None, author=User(id=9, username="author"))

        await publisher._create_merge_request("feature", "Title", "Body")

        assert publisher.client.update_or_create_merge_request.call_args.kwargs["assignee_id"] == 9

    async def test_falls_back_to_author_username_on_github(self):
        publisher = _make_publisher(git_platform=GitPlatform.GITHUB)
        publisher.ctx.issue = self._issue(assignee=None, author=User(id=9, username="author"))

        await publisher._create_merge_request("feature", "Title", "Body")

        assert publisher.client.update_or_create_merge_request.call_args.kwargs["assignee_id"] == "author"

    async def test_no_assignee_when_no_issue(self):
        publisher = _make_publisher(git_platform=GitPlatform.GITLAB)
        publisher.ctx.issue = None

        await publisher._create_merge_request("feature", "Title", "Body")

        assert publisher.client.update_or_create_merge_request.call_args.kwargs["assignee_id"] is None


class TestBuildIssueCreationUrl:
    def test_gitlab_url_format(self):
        publisher = _make_publisher(git_platform=GitPlatform.GITLAB)

        url = publisher._build_issue_creation_url("AGENTS.md")

        parsed = urlparse(url)
        assert parsed.path == "/owner/repo/-/issues/new"
        params = parse_qs(parsed.query)
        assert "issue[title]" in params
        assert "AGENTS.md" in params["issue[title]"][0]
        assert "issue[description]" in params
        assert "/label ~" + BOT_AUTO_LABEL in params["issue[description]"][0]

    def test_github_url_format(self):
        publisher = _make_publisher(git_platform=GitPlatform.GITHUB)

        url = publisher._build_issue_creation_url("AGENTS.md")

        parsed = urlparse(url)
        assert parsed.path == "/owner/repo/issues/new"
        params = parse_qs(parsed.query)
        assert "title" in params
        assert "AGENTS.md" in params["title"][0]
        assert "body" in params
        assert "labels" in params
        assert params["labels"][0] == BOT_AUTO_LABEL


class TestPublishLocalAuthEnv:
    async def test_local_mode_overlays_client_credential_env(self, monkeypatch):
        """Sandbox-disabled publishes push from the DAIV-container clone, whose .git/config no
        longer holds a credential — the publisher must fetch the per-run env from the repo client
        and open the local manager with it."""
        publisher = _make_publisher()
        auth_env = GitAuthEnv.for_token("https://gitlab.com/owner/repo.git", "tok")
        publisher.client.get_git_auth_env.return_value = auth_env
        captured = _patch_open_git_manager(monkeypatch, _fake_git_manager(dirty=False, diff=""))

        await publisher.publish(merge_request=None)

        assert captured["auth_env"] is auth_env
        publisher.client.get_git_auth_env.assert_called_once_with(publisher.ctx.repository)

    async def test_sandbox_mode_skips_credential_env(self, monkeypatch):
        """Sandbox git authenticates via the egress proxy's injected header; minting a token here
        would be a needless platform API call and a needless secret in memory."""
        publisher = _make_sandbox_publisher(egress=None)  # no egress proxy → the pre-publish refresh no-ops
        captured = _patch_open_git_manager(monkeypatch, _fake_git_manager(dirty=False, diff=""))

        await publisher.publish(merge_request=None)

        assert captured["auth_env"] is None
        publisher.client.get_git_auth_env.assert_not_called()


class TestPublishSandboxEgressRefresh:
    async def test_sandbox_publish_refreshes_egress_before_git_ops(self, monkeypatch):
        """Sandbox publishes re-mint the platform token and deliver it onto the live session BEFORE
        the first in-sandbox network git op, so a turn that outlived the turn-start token (GitHub
        installation tokens live 1h) still pushes with a fresh credential."""
        from automation.agent.git_manager import RepoStatus
        from core.sandbox.schemas import EgressConfigRequest

        publisher = _make_sandbox_publisher()
        fresh = EgressConfigRequest()
        remint = Mock(return_value=fresh)
        monkeypatch.setattr("sandbox_envs.services.refresh_platform_egress", remint)

        order: list[str] = []
        publisher.sandbox_backend.refresh_egress = AsyncMock(side_effect=lambda _egress: order.append("refresh"))
        gm = _fake_git_manager()
        gm.status_snapshot = AsyncMock(
            side_effect=lambda **_kw: (
                order.append("snapshot") or RepoStatus(dirty=False, diff="", remote_branches=[], has_unpushed=False)
            )
        )
        _patch_open_git_manager(monkeypatch, gm)

        await publisher.publish(merge_request=None)

        # Pin the wiring, not just the call: a wrong argument (e.g. `sandbox` instead of
        # `sandbox.egress`) would be swallowed by the best-effort except in production and silently
        # disable the feature on every publish, while a mock with a fixed return stays green.
        remint.assert_called_once_with(publisher.ctx.sandbox.egress, publisher.client, publisher.ctx.repository)
        publisher.sandbox_backend.refresh_egress.assert_awaited_once_with(fresh)
        assert order == ["refresh", "snapshot"]

    async def test_local_mode_does_not_refresh_egress(self, monkeypatch):
        """Local mode has no live egress proxy to refresh — its credential is a fresh
        per-invocation ``auth_env`` overlay instead."""
        publisher = _make_publisher()  # sandbox_backend defaults to None
        publisher._refresh_sandbox_egress = AsyncMock()
        _patch_open_git_manager(monkeypatch, _fake_git_manager(dirty=False, diff=""))

        await publisher.publish(merge_request=None)

        publisher._refresh_sandbox_egress.assert_not_awaited()

    async def test_publish_proceeds_when_refresh_fails(self, monkeypatch, caplog):
        """A failed refresh (e.g. the GitHub re-mint errors) must not abort the publish — it
        degrades to publishing with the turn-start token, the pre-existing behavior — but the
        failure must stay diagnosable (exception-logged), or the degradation is truly silent."""
        publisher = _make_sandbox_publisher()
        monkeypatch.setattr(
            "sandbox_envs.services.refresh_platform_egress", Mock(side_effect=RuntimeError("mint failed"))
        )
        _patch_open_git_manager(monkeypatch, _fake_git_manager(dirty=False, diff=""))

        with caplog.at_level("ERROR", logger="daiv.tools"):
            outcome = await publisher.publish(merge_request=None)

        assert outcome == PublishOutcome(merge_request=None, published=False, diff_stats=_LOCAL_STATS)
        assert "Could not refresh the sandbox egress token" in caplog.text

    async def test_refresh_skips_delivery_when_nothing_to_refresh(self, monkeypatch):
        publisher = _make_sandbox_publisher()

        # refresh_platform_egress returns the same object when there is no token to rotate (no
        # proxy, token-less platform, or an identical re-mint — e.g. GitLab's day-cached token).
        monkeypatch.setattr(
            "sandbox_envs.services.refresh_platform_egress", Mock(return_value=publisher.ctx.sandbox.egress)
        )

        await publisher._refresh_sandbox_egress()

        publisher.sandbox_backend.refresh_egress.assert_not_awaited()

    async def test_refresh_swallows_delivery_error(self, monkeypatch, caplog):
        # The failure is in the sidecar DELIVERY (mint succeeded): swallow it — but exception-log
        # it — and proceed with the turn-start token rather than failing the publish.
        import httpx

        from core.sandbox.schemas import EgressConfigRequest

        publisher = _make_sandbox_publisher()
        publisher.sandbox_backend.refresh_egress = AsyncMock(side_effect=httpx.ConnectError("down"))
        monkeypatch.setattr("sandbox_envs.services.refresh_platform_egress", Mock(return_value=EgressConfigRequest()))

        with caplog.at_level("ERROR", logger="daiv.tools"):
            await publisher._refresh_sandbox_egress()  # must not raise

        publisher.sandbox_backend.refresh_egress.assert_awaited_once()
        assert "Could not refresh the sandbox egress token" in caplog.text


class TestPublishSuggestsContextFile:
    @pytest.fixture
    def publisher(self):
        pub = _make_publisher()
        pub.client.get_repository_file.return_value = None
        return pub

    async def test_calls_suggest_on_new_mr(self, publisher, monkeypatch):
        mr = _make_merge_request()
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(publisher, "_create_merge_request", return_value=mr),
            patch.object(publisher, "_suggest_context_file") as mock_suggest,
        ):
            result = await publisher.publish(merge_request=None)

            gm.commit_all.assert_awaited_once()
            # Fresh branch for a brand-new MR: no remote work to integrate, so no rebase-on-reject.
            gm.push_head_to.assert_awaited_once_with("feature", integrate_on_reject=False, skip_ci=False)
            mock_suggest.assert_called_once_with(mr)
            assert result == PublishOutcome(merge_request=mr, published=True, diff_stats=_LOCAL_STATS)

    async def test_degrades_to_pending_when_branch_not_visible(self, publisher, monkeypatch, caplog):
        """A pushed branch GitLab won't open an MR for degrades to a partial publish, not a failed job.

        The work is already committed/pushed; surfacing the recoverable error as ``merge_request=None,
        published=True`` lets the run complete (agent reply preserved) instead of orphaning the branch.
        """
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(publisher, "_create_merge_request", side_effect=MergeRequestBranchNotVisibleError("feature")),
            patch.object(publisher, "_suggest_context_file") as mock_suggest,
            caplog.at_level("ERROR", logger="daiv.tools"),
        ):
            result = await publisher.publish(merge_request=None)

        gm.commit_all.assert_awaited_once()
        gm.push_head_to.assert_awaited_once_with("feature", integrate_on_reject=False, skip_ci=False)
        mock_suggest.assert_not_called()
        assert result == PublishOutcome(merge_request=None, published=True, diff_stats=_LOCAL_STATS)
        assert "MR pending" in caplog.text

    async def test_degrade_preserves_protected_branch_fallback_source(self, publisher, monkeypatch):
        """The compound case: a protected source branch forced a fresh branch, which then hit the
        visibility race. The degrade outcome must still carry the fallback source."""
        existing_mr = _make_merge_request(source_branch="dev", merge_request_id=42)
        publisher.client.is_branch_protected.return_value = True
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature-fix", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(
                publisher, "_create_merge_request", side_effect=MergeRequestBranchNotVisibleError("feature-fix")
            ),
        ):
            result = await publisher.publish(merge_request=existing_mr)

        assert result == PublishOutcome(
            merge_request=None, published=True, protected_branch_fallback_source="dev", diff_stats=_LOCAL_STATS
        )

    async def test_falls_back_to_new_mr_when_source_branch_protected(self, publisher, monkeypatch):
        existing_mr = _make_merge_request(source_branch="dev", target_branch="release/x", merge_request_id=42)
        new_mr = _make_merge_request(source_branch="feature-fix", merge_request_id=43)
        publisher.client.is_branch_protected.return_value = True
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature-fix", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ) as mock_diff_to_metadata,
            patch.object(publisher, "_create_merge_request", return_value=new_mr) as mock_create_mr,
            patch.object(publisher, "_suggest_context_file"),
        ):
            result = await publisher.publish(merge_request=existing_mr)

            publisher.client.is_branch_protected.assert_called_once_with("owner/repo", "dev")
            # Pre-check must run before _diff_to_metadata so the fallback path receives a
            # populated pr_metadata_diff (the new MR needs title/branch/description).
            assert mock_diff_to_metadata.call_args.kwargs["pr_metadata_diff"] is not None
            # Fresh unique branch generated + pushed for the fallback MR (no remote work to integrate).
            gm.unique_branch_name.assert_called_once_with("feature-fix", [])
            gm.push_head_to.assert_awaited_once_with("feature-fix", integrate_on_reject=False, skip_ci=False)
            # The new MR is created with a back-link to the original protected MR.
            mock_create_mr.assert_called_once()
            assert mock_create_mr.call_args.kwargs["fallback_from_mr"] is existing_mr
            # The original MR's non-default target flows through to the fallback (paired with the unit
            # test that _create_merge_request derives target_branch from it).
            assert mock_create_mr.call_args.kwargs["fallback_from_mr"].target_branch == "release/x"
            # No fallback comment is posted from the publisher itself.
            publisher.client.create_merge_request_comment.assert_not_called()
            # Fallback source is exposed on the outcome so the manager can bundle a footer onto the
            # agent's reply instead of posting a separate comment.
            assert result == PublishOutcome(
                merge_request=new_mr, published=True, protected_branch_fallback_source="dev", diff_stats=_LOCAL_STATS
            )

    async def test_protected_branch_fallback_is_per_call(self, publisher, monkeypatch):
        # The fallback source lives on the per-call PublishOutcome, so a protected-branch call that
        # reports it cannot leak the signal into a later clean publish.
        existing_mr = _make_merge_request(source_branch="dev", merge_request_id=42)
        new_mr = _make_merge_request(source_branch="feature-fix", merge_request_id=43)
        publisher.client.is_branch_protected.return_value = True
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature-fix", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(publisher, "_create_merge_request", return_value=new_mr),
            patch.object(publisher, "_suggest_context_file"),
        ):
            first = await publisher.publish(merge_request=existing_mr)
            assert first.protected_branch_fallback_source == "dev"

            # Second call: clean tree, no diff versus base → publish short-circuits, and the
            # outcome carries no fallback source.
            gm.status_snapshot.return_value = RepoStatus(dirty=False, diff="", remote_branches=[], has_unpushed=False)
            second = await publisher.publish(merge_request=None)
            assert second.protected_branch_fallback_source is None

    async def test_publish_propagates_push_failure_without_creating_mr(self, publisher, monkeypatch):
        """If the daiv-direct push fails, publish() must propagate (fail loud) and NOT open an MR
        against a branch that never landed on the remote."""
        from automation.agent.git_manager import GitPushNetworkError

        gm = _fake_git_manager()
        gm.push_head_to = AsyncMock(side_effect=GitPushNetworkError("no network"))
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(publisher, "_create_merge_request") as mock_create,
            patch.object(publisher, "_suggest_context_file") as mock_suggest,
        ):
            with pytest.raises(GitPushNetworkError):
                await publisher.publish(merge_request=None)

            mock_create.assert_not_called()
            mock_suggest.assert_not_called()

    async def test_does_not_suggest_on_existing_mr(self, publisher, monkeypatch):
        mr = _make_merge_request()
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feature", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(publisher, "_suggest_context_file") as mock_suggest,
        ):
            await publisher.publish(merge_request=mr)

            # Existing MR: push to its source branch, integrating remote work on a non-ff rejection
            # (the branch may have moved under the run, e.g. a concurrent push).
            gm.push_head_to.assert_awaited_once_with("feature", integrate_on_reject=True, skip_ci=False)
            mock_suggest.assert_not_called()


class TestPublishDecision:
    """The publish decision (formerly GitMiddleware._is_unpublished) now lives in publish()."""

    async def test_returns_nothing_when_clean_and_no_diff(self, monkeypatch):
        publisher = _make_publisher()
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with patch.object(publisher, "_diff_to_metadata") as meta:
            outcome = await publisher.publish(merge_request=None)

        assert outcome == PublishOutcome(merge_request=None, published=False, diff_stats=_LOCAL_STATS)
        meta.assert_not_called()
        gm.push_head_to.assert_not_called()

    async def test_confirms_existing_mr_without_republishing_when_pushed(self, monkeypatch):
        publisher = _make_publisher()
        mr = _make_merge_request(source_branch="feat/x", merge_request_id=42)
        gm = _fake_git_manager(dirty=False, diff="diff", remote_branches=["feat/x"], has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with patch.object(publisher, "_diff_to_metadata") as meta:
            outcome = await publisher.publish(merge_request=mr)

        assert outcome == PublishOutcome(merge_request=mr, published=False, diff_stats=_LOCAL_STATS)
        meta.assert_not_called()
        gm.push_head_to.assert_not_called()

    async def test_diffs_against_existing_mr_target_branch(self, monkeypatch):
        """An in-review MR targeting a non-default branch is diffed against that branch, not main."""
        publisher = _make_publisher()
        mr = _make_merge_request(source_branch="feat/x", target_branch="release/x", merge_request_id=42)
        gm = _fake_git_manager(dirty=False, diff="diff", remote_branches=["feat/x"], has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with patch.object(publisher, "_diff_to_metadata") as meta:
            outcome = await publisher.publish(merge_request=mr)

        assert gm.status_snapshot.await_args.kwargs["base_branch"] == "release/x"
        assert gm.status_snapshot.await_args.kwargs["mr_source_branch"] == "feat/x"
        # Clean tree already on its MR → no duplicate MR, no metadata call.
        assert outcome == PublishOutcome(merge_request=mr, published=False, diff_stats=_LOCAL_STATS)
        meta.assert_not_called()

    async def test_diffs_against_existing_mr_target_branch_on_dirty_tree(self, monkeypatch):
        """A dirty tree on a non-default-target MR still diffs against that target and proceeds to
        commit/push — guards against a regression recomputing the base below the clean short-circuit."""
        publisher = _make_publisher()
        mr = _make_merge_request(source_branch="feat/x", target_branch="release/x", merge_request_id=42)
        gm = _fake_git_manager()  # dirty=True, has_unpushed=True → publish proceeds
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feat/x", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ) as meta,
            patch.object(publisher, "_suggest_context_file"),
        ):
            outcome = await publisher.publish(merge_request=mr)

        assert gm.status_snapshot.await_args.kwargs["base_branch"] == "release/x"
        meta.assert_called_once()  # proceeded past the clean short-circuit
        gm.push_head_to.assert_awaited_once_with("feat/x", integrate_on_reject=True, skip_ci=False)
        assert outcome == PublishOutcome(merge_request=mr, published=True, diff_stats=_LOCAL_STATS)

    async def test_diffs_against_default_branch_when_no_mr(self, monkeypatch):
        publisher = _make_publisher()
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with patch.object(publisher, "_diff_to_metadata"):
            await publisher.publish(merge_request=None)

        assert gm.status_snapshot.await_args.kwargs["base_branch"] == "main"


def test_create_merge_request_and_suggest_are_async():
    assert inspect.iscoroutinefunction(GitChangePublisher._create_merge_request)
    assert inspect.iscoroutinefunction(GitChangePublisher._suggest_context_file)


class TestTriggerServiceAccountPipeline:
    async def test_triggers_pipeline_via_client(self):
        publisher = _make_publisher()
        publisher.client.trigger_merge_request_pipeline = Mock(return_value=Mock(id=5))
        mr = _make_merge_request()

        await publisher._trigger_service_account_pipeline(mr)

        publisher.client.trigger_merge_request_pipeline.assert_called_once_with("owner/repo", 42)
        publisher.client.create_merge_request_comment.assert_not_called()

    async def test_logs_and_notes_on_failure(self, caplog):
        publisher = _make_publisher()
        publisher.client.trigger_merge_request_pipeline = Mock(side_effect=RuntimeError("boom"))
        mr = _make_merge_request()

        with caplog.at_level("ERROR"):
            await publisher._trigger_service_account_pipeline(mr)

        assert any(r.levelname == "ERROR" for r in caplog.records)
        publisher.client.create_merge_request_comment.assert_called_once()
        body = publisher.client.create_merge_request_comment.call_args[0][2]
        assert "pipeline" in body.lower()

    async def test_does_not_raise_when_note_also_fails(self):
        publisher = _make_publisher()
        publisher.client.trigger_merge_request_pipeline = Mock(side_effect=RuntimeError("boom"))
        publisher.client.create_merge_request_comment = Mock(side_effect=RuntimeError("note failed"))

        await publisher._trigger_service_account_pipeline(_make_merge_request())  # must not raise


class TestPublishPipelineHeal:
    """GitLab ephemeral-token pushes skip the bot pipeline and trigger it as the service account."""

    async def test_ephemeral_push_skips_ci_and_triggers_pipeline(self, monkeypatch):
        publisher = _make_publisher(git_platform=GitPlatform.GITLAB)
        publisher.client.push_uses_ephemeral_token.return_value = True
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)
        mr = _make_merge_request()

        with (
            patch.object(publisher, "_diff_to_metadata", return_value=_metadata_stub()),
            patch.object(publisher, "_create_merge_request", return_value=mr),
            patch.object(publisher, "_suggest_context_file", AsyncMock()),
            patch.object(publisher, "_trigger_service_account_pipeline", AsyncMock()) as trigger,
        ):
            await publisher.publish()

        gm.push_head_to.assert_awaited_once_with("feature", integrate_on_reject=False, skip_ci=True)
        trigger.assert_awaited_once_with(mr)

    async def test_pat_push_does_not_skip_ci_or_trigger(self, monkeypatch):
        publisher = _make_publisher(git_platform=GitPlatform.GITLAB)
        publisher.client.push_uses_ephemeral_token.return_value = False
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)
        mr = _make_merge_request()

        with (
            patch.object(publisher, "_diff_to_metadata", return_value=_metadata_stub()),
            patch.object(publisher, "_create_merge_request", return_value=mr),
            patch.object(publisher, "_suggest_context_file", AsyncMock()),
            patch.object(publisher, "_trigger_service_account_pipeline", AsyncMock()) as trigger,
        ):
            await publisher.publish()

        gm.push_head_to.assert_awaited_once_with("feature", integrate_on_reject=False, skip_ci=False)
        trigger.assert_not_awaited()

    async def test_github_never_heals(self, monkeypatch):
        publisher = _make_publisher(git_platform=GitPlatform.GITHUB)
        # GitHub's client reports no ephemeral token (base default), so the polymorphic heal never
        # fires — the publisher special-cases no platform.
        publisher.client.push_uses_ephemeral_token.return_value = False
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)
        mr = _make_merge_request()

        with (
            patch.object(publisher, "_diff_to_metadata", return_value=_metadata_stub()),
            patch.object(publisher, "_create_merge_request", return_value=mr),
            patch.object(publisher, "_suggest_context_file", AsyncMock()),
            patch.object(publisher, "_trigger_service_account_pipeline", AsyncMock()) as trigger,
        ):
            await publisher.publish()

        gm.push_head_to.assert_awaited_once_with("feature", integrate_on_reject=False, skip_ci=False)
        trigger.assert_not_awaited()

    async def test_skip_ci_flag_disables_heal(self, monkeypatch):
        publisher = _make_publisher(git_platform=GitPlatform.GITLAB)
        publisher.client.push_uses_ephemeral_token.return_value = True
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)
        mr = _make_merge_request()

        with (
            patch.object(publisher, "_diff_to_metadata", return_value=_metadata_stub()),
            patch.object(publisher, "_create_merge_request", return_value=mr),
            patch.object(publisher, "_suggest_context_file", AsyncMock()),
            patch.object(publisher, "_trigger_service_account_pipeline", AsyncMock()) as trigger,
        ):
            await publisher.publish(skip_ci=True)

        # explicit skip_ci means "no CI at all" — no bot pipeline, no service-account trigger
        gm.push_head_to.assert_awaited_once_with("feature", integrate_on_reject=False, skip_ci=False)
        trigger.assert_not_awaited()


class TestPublishDiffStats:
    """``+x −y`` for the composer's progress pill: counted from the snapshot the publish
    already took, never fetched back from the platform."""

    async def test_counts_the_snapshot_diff(self, monkeypatch):
        publisher = _make_publisher()
        mr = _make_merge_request()
        gm = _fake_git_manager(diff="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n+one\n+two\n-three\n")
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(publisher, "_diff_to_metadata", return_value=_metadata_stub()),
            patch.object(publisher, "_create_merge_request", return_value=mr),
            patch.object(publisher, "_suggest_context_file"),
        ):
            outcome = await publisher.publish(merge_request=None)

        assert outcome.diff_stats == MergeRequestDiffStats(lines_added=2, lines_removed=1, files_changed=1)

    async def test_never_asks_the_platform_for_the_numbers(self, monkeypatch):
        """The snapshot diff is the merge-base delta for the branch the MR is built from,
        so it already is what the MR page shows. Fetching it back costs a full MR-diff
        download on GitLab, truncates past a size cap, and answers zero while a
        just-created MR is still being prepared."""
        publisher = _make_publisher()
        mr = _make_merge_request()
        _patch_open_git_manager(monkeypatch, _fake_git_manager())

        with (
            patch.object(publisher, "_diff_to_metadata", return_value=_metadata_stub()),
            patch.object(publisher, "_create_merge_request", return_value=mr),
            patch.object(publisher, "_suggest_context_file"),
        ):
            await publisher.publish(merge_request=None)

        publisher.client.get_merge_request_diff_stats.assert_not_called()

    async def test_no_changes_reports_zeros(self, monkeypatch):
        """State 02 — the composer renders zeros as no pill; ``None`` would leave a previous
        turn's numbers standing."""
        publisher = _make_publisher()
        _patch_open_git_manager(monkeypatch, _fake_git_manager(dirty=False, diff="", has_unpushed=False))

        outcome = await publisher.publish(merge_request=None)

        assert outcome.diff_stats == _LOCAL_STATS
