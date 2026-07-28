import inspect
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from automation.agent.git_manager import RepoStatus
from automation.agent.publishers import GitChangePublisher, PublishOutcome
from codebase.base import GitPlatform, MergeRequest, User
from codebase.clients.base import GitAuthEnv
from codebase.exceptions import MergeRequestBranchNotVisibleError
from core.constants import BOT_AUTO_LABEL, BOT_NAME


def _fake_git_manager(
    *, dirty: bool = True, diff: str = "diff", remote_branches=(), has_unpushed: bool = True, diff_base: str = "main"
) -> Mock:
    """A stand-in for the (sandbox/local) GitManager the publisher opens via open_git_manager.

    The publisher reads everything it needs from a single ``status_snapshot``; the mutation methods
    (``commit_all``/``push_head_to``) stay separate AsyncMocks.
    """
    gm = Mock()
    gm.status_snapshot = AsyncMock(
        return_value=RepoStatus(
            dirty=dirty,
            diff=diff,
            remote_branches=list(remote_branches),
            has_unpushed=has_unpushed,
            diff_base=diff_base,
        )
    )
    gm.commit_all = AsyncMock()
    gm.push_head_to = AsyncMock(return_value="pushed")
    gm.unique_branch_name = Mock(side_effect=lambda name, existing: name)
    gm.get_range_diff = AsyncMock(return_value="range diff")
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


def _make_publisher(*, git_platform: GitPlatform = GitPlatform.GITLAB, context_file_name: str | None = "AGENTS.md"):
    ctx = Mock()
    ctx.repository.slug = "owner/repo"
    ctx.repository.html_url = "https://gitlab.com/owner/repo"
    ctx.repository.git_platform = git_platform
    ctx.config.context_file_name = context_file_name
    ctx.config.suggest_context_file = True
    ctx.config.default_branch = "main"
    ctx.git_platform = git_platform

    if git_platform == GitPlatform.GITHUB:
        ctx.repository.html_url = "https://github.com/owner/repo"

    publisher = GitChangePublisher(ctx)
    publisher.client = Mock()
    publisher.client.is_branch_protected.return_value = False
    return publisher


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


class TestCreateMergeRequestDescription:
    """The new MR description back-links to the original protected MR for traceability."""

    def _make_publisher_with_no_issue(self, *, git_platform: GitPlatform = GitPlatform.GITLAB) -> GitChangePublisher:
        publisher = _make_publisher(git_platform=git_platform)
        publisher.ctx.issue = None
        publisher.ctx.bot_username = "daiv"
        return publisher

    async def test_includes_back_link_when_fallback_provided(self):
        publisher = self._make_publisher_with_no_issue()
        original = _make_merge_request(
            source_branch="dev", merge_request_id=42, web_url="https://gitlab.com/owner/repo/-/merge_requests/42"
        )

        await publisher._create_merge_request("feature-fix", "Title", "Body", as_draft=False, fallback_from_mr=original)

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "dev" in description
        assert "https://gitlab.com/owner/repo/-/merge_requests/42" in description
        assert "!42" in description

    async def test_omits_back_link_when_no_fallback(self):
        publisher = self._make_publisher_with_no_issue()

        await publisher._create_merge_request("feature", "Title", "Body", as_draft=False)

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "is protected on the remote" not in description

    async def test_targets_the_repo_default_branch_by_default(self):
        publisher = self._make_publisher_with_no_issue()

        await publisher._create_merge_request("feature", "Title", "Body", as_draft=False)

        assert publisher.client.update_or_create_merge_request.call_args.kwargs["target_branch"] == "main"

    async def test_inherits_the_target_branch_of_the_mr_it_replaces(self):
        """The replacement MR reviews the same change against the same base.

        Re-pointing it at the repo default would silently turn a release-branch review into a much
        larger default-branch one.
        """
        publisher = self._make_publisher_with_no_issue()
        original = _make_merge_request(source_branch="dev", target_branch="release/phase-4", merge_request_id=42)

        await publisher._create_merge_request("feature-fix", "Title", "Body", as_draft=False, fallback_from_mr=original)

        kwargs = publisher.client.update_or_create_merge_request.call_args.kwargs
        assert kwargs["target_branch"] == "release/phase-4"

    async def test_back_link_uses_github_terminology(self):
        publisher = self._make_publisher_with_no_issue(git_platform=GitPlatform.GITHUB)
        original = _make_merge_request(
            source_branch="main", merge_request_id=10, web_url="https://github.com/owner/repo/pull/10"
        )

        await publisher._create_merge_request("feature-fix", "Title", "Body", as_draft=False, fallback_from_mr=original)

        description = publisher.client.update_or_create_merge_request.call_args.kwargs["description"]
        assert "#10" in description
        assert "!10" not in description


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
                order.append("snapshot")
                or RepoStatus(dirty=False, diff="", remote_branches=[], has_unpushed=False, diff_base="main")
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

        assert outcome == PublishOutcome(merge_request=None, published=False)
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
            gm.push_head_to.assert_awaited_once_with("feature", integrate_on_reject=False)
            mock_suggest.assert_called_once_with(mr)
            assert result == PublishOutcome(merge_request=mr, published=True)

    async def test_falls_back_to_new_mr_when_source_branch_protected(self, publisher, monkeypatch):
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
            gm.push_head_to.assert_awaited_once_with("feature-fix", integrate_on_reject=False)
            # The new MR is created with a back-link to the original protected MR.
            mock_create_mr.assert_called_once()
            assert mock_create_mr.call_args.kwargs["fallback_from_mr"] is existing_mr
            # No fallback comment is posted from the publisher itself.
            publisher.client.create_merge_request_comment.assert_not_called()
            # Fallback source is exposed on the outcome so the manager can bundle a footer onto the
            # agent's reply instead of posting a separate comment.
            assert result == PublishOutcome(
                merge_request=new_mr, published=True, protected_branch_fallback_source="dev"
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
            gm.status_snapshot.return_value = RepoStatus(
                dirty=False, diff="", remote_branches=[], has_unpushed=False, diff_base="main"
            )
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
            gm.push_head_to.assert_awaited_once_with("feature", integrate_on_reject=True)
            mock_suggest.assert_not_called()


class TestPublishDiffBase:
    """Which branch the run's work is diffed against — the review base, not always the default."""

    async def test_diffs_against_the_default_branch_without_an_mr(self, monkeypatch):
        publisher = _make_publisher()
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
            patch.object(publisher, "_create_merge_request", return_value=_make_merge_request()),
            patch.object(publisher, "_suggest_context_file"),
        ):
            await publisher.publish(merge_request=None)

        assert gm.status_snapshot.await_args.kwargs["base_branch"] == "main"

    async def test_diffs_against_the_mr_target_branch(self, monkeypatch):
        """A run inside an MR is reviewed against that MR's target.

        Diffing the repo default instead pulled in every commit separating the two branches, so a
        small change on a branch stacked off a release branch reached diff_to_metadata as that
        release branch's whole history — and the commit message and generated branch name then
        described that history rather than the run's change.
        """
        publisher = _make_publisher()
        mr = _make_merge_request(source_branch="feat/x", target_branch="release/phase-4")
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="feat/x", title="Title", description="Desc"),
                    "commit_message": Mock(commit_message="commit msg"),
                },
            ),
            patch.object(publisher, "_suggest_context_file"),
        ):
            await publisher.publish(merge_request=mr)

        kwargs = gm.status_snapshot.await_args.kwargs
        assert kwargs["base_branch"] == "release/phase-4"
        # The repo default rides along as the fallback: a target branch deleted on the remote after
        # the clone must not abort the publish and strand the run's work.
        assert kwargs["fallback_base_branch"] == "main"


class TestPublishOutcomeInvariants:
    """The class docstring claims these are "checked rather than merely documented" — pin that.

    Every consumer branches on this combination to decide what the user is told and what gets
    checkpointed, and there are two independent writers, so an illegal combination silently reports the
    wrong thing. Deleting the guards is otherwise invisible: 25 construction sites exercise only the
    passing direction.
    """

    def test_rejects_a_pending_branch_alongside_an_open_mr(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            PublishOutcome(merge_request=_make_merge_request(), published=True, pending_branch="daiv/x")

    def test_rejects_an_unpublished_pending_branch(self):
        with pytest.raises(ValueError, match="published must be True"):
            PublishOutcome(merge_request=None, published=False, pending_branch="daiv/x")

    def test_rejects_a_published_turn_that_says_nothing_about_where_the_work_went(self):
        with pytest.raises(ValueError, match="must report where the work went"):
            PublishOutcome(merge_request=None, published=True)

    def test_rejects_a_verified_flag_with_no_branch_to_describe(self):
        with pytest.raises(ValueError, match="needs one"):
            PublishOutcome(merge_request=None, published=False, pending_branch_verified=True)

    def test_accepts_the_four_legal_shapes(self):
        mr = _make_merge_request()
        assert PublishOutcome(merge_request=None, published=False).published is False
        assert PublishOutcome(merge_request=mr, published=False).merge_request is mr
        assert PublishOutcome(merge_request=mr, published=True).published is True
        assert (
            PublishOutcome(
                merge_request=None, published=True, pending_branch="daiv/x", pending_branch_verified=True
            ).pending_branch
            == "daiv/x"
        )


class TestPublishOutcomeStateUpdate:
    """Both state writers go through this, so it is the one place the interlocking keys are decided."""

    def test_an_mr_settles_an_outstanding_debt(self):
        update = PublishOutcome(merge_request=_make_merge_request(), published=True).state_update(
            had_pending_branch=True
        )
        assert update["pending_mr_branch"] is None
        assert update["pending_mr_branch_verified"] is False
        assert update["code_changes"] is True

    def test_settling_a_debt_keeps_the_reason_the_branch_existed(self):
        """The fallback happened on the turn that created the owed branch, so the turn that finally opens
        the MR must not overwrite it with its own `None` — that is the only explanation the reviewer gets
        for being sent to a different merge request."""
        update = PublishOutcome(merge_request=_make_merge_request(), published=True).state_update(
            had_pending_branch=True
        )
        assert "protected_branch_fallback_source" not in update

    def test_a_fresh_publish_records_its_own_fallback_signal(self):
        update = PublishOutcome(
            merge_request=_make_merge_request(), published=True, protected_branch_fallback_source="release/1.2"
        ).state_update(had_pending_branch=False)
        assert update["protected_branch_fallback_source"] == "release/1.2"

    def test_a_pending_branch_invalidates_the_state_mr(self):
        update = PublishOutcome(merge_request=None, published=True, pending_branch="daiv/x").state_update(
            had_pending_branch=False
        )
        assert update["merge_request"] is None
        assert update["pending_mr_branch"] == "daiv/x"

    def test_nothing_published_expires_only_an_existing_debt(self):
        outcome = PublishOutcome(merge_request=None, published=False)
        assert outcome.state_update(had_pending_branch=True) == {
            "pending_mr_branch": None,
            "pending_mr_branch_verified": False,
        }
        assert outcome.state_update(had_pending_branch=False) == {}


class TestPublishDiffBaseSubstitution:
    async def test_reports_when_the_diff_base_was_substituted(self, monkeypatch, caplog):
        """A substituted base must not pass unremarked: it silently changes the generated metadata.

        This whole change set exists because diffing the wrong base makes the commit message, PR
        description and branch name describe unrelated history. The fallback deliberately re-enters that
        state, so the publisher — the thing that then generates all three — has to say so.
        """

        publisher = _make_publisher()
        mr = _make_merge_request(source_branch="feat/x", target_branch="release/gone")
        gm = _fake_git_manager(diff_base="main")
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="b", title="T", description="D"),
                    "commit_message": Mock(commit_message="m"),
                },
            ),
            caplog.at_level(logging.WARNING),
        ):
            await publisher.publish(merge_request=mr)

        assert "release/gone" in caplog.text
        assert "main" in caplog.text

    async def test_silent_when_the_requested_base_was_used(self, monkeypatch, caplog):

        publisher = _make_publisher()
        mr = _make_merge_request(source_branch="feat/x", target_branch="release/1.2")
        _patch_open_git_manager(monkeypatch, _fake_git_manager(diff_base="release/1.2"))

        with (
            patch.object(
                publisher,
                "_diff_to_metadata",
                return_value={
                    "pr_metadata": Mock(branch="b", title="T", description="D"),
                    "commit_message": Mock(commit_message="m"),
                },
            ),
            caplog.at_level(logging.WARNING),
        ):
            await publisher.publish(merge_request=mr)

        assert "generated against" not in caplog.text


class TestPublishPendingBranch:
    """Pushed work whose MR the platform refused to open must survive the turn."""

    def _metadata(self, publisher, branch: str = "feature"):
        return patch.object(
            publisher,
            "_diff_to_metadata",
            return_value={
                "pr_metadata": Mock(branch=branch, title="Title", description="Desc"),
                "commit_message": Mock(commit_message="commit msg"),
            },
        )

    async def test_reports_the_branch_instead_of_failing(self, monkeypatch):

        publisher = _make_publisher()
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            self._metadata(publisher),
            patch.object(
                publisher,
                "_create_merge_request",
                side_effect=MergeRequestBranchNotVisibleError("feature", verified=True),
            ),
            patch.object(publisher, "_suggest_context_file") as mock_suggest,
        ):
            outcome = await publisher.publish(merge_request=None)

        # published=True: the commits are on the remote, only the MR is missing.
        assert outcome == PublishOutcome(
            merge_request=None, published=True, pending_branch="feature", pending_branch_verified=True
        )
        gm.push_head_to.assert_awaited_once()
        mock_suggest.assert_not_called()

    async def test_reuses_the_pending_branch_on_a_later_turn(self, monkeypatch):
        publisher = _make_publisher()
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with (
            self._metadata(publisher, branch="freshly-generated"),
            patch.object(publisher, "_create_merge_request", return_value=_make_merge_request()),
            patch.object(publisher, "_suggest_context_file"),
        ):
            await publisher.publish(merge_request=None, pending_branch="daiv/owed-an-mr")

        # The owed branch is pushed to again — no new name minted beside it — and remote work is
        # integrated, since a re-cloned workspace is not a descendant of the earlier turn's commit.
        gm.unique_branch_name.assert_not_called()
        gm.push_head_to.assert_awaited_once_with("daiv/owed-an-mr", integrate_on_reject=True)

    async def test_records_that_the_branch_could_not_be_confirmed(self, monkeypatch):
        """An unverified branch must stay distinguishable all the way to the reply.

        Telling someone their work is safe is what stops them redoing it, so "we could not check"
        cannot be reported as "confirmed on the remote".
        """

        publisher = _make_publisher()
        _patch_open_git_manager(monkeypatch, _fake_git_manager())

        with (
            self._metadata(publisher),
            patch.object(
                publisher,
                "_create_merge_request",
                side_effect=MergeRequestBranchNotVisibleError("feature", verified=False),
            ),
            patch.object(publisher, "_suggest_context_file"),
        ):
            outcome = await publisher.publish(merge_request=None)

        assert outcome.pending_branch == "feature"
        assert outcome.pending_branch_verified is False

    async def test_opens_the_owed_mr_when_the_turn_changed_nothing(self, monkeypatch):
        """The advertised remedy ("re-run and DAIV opens it on that branch") must actually happen.

        An issue-scope re-run starts from a fresh clone of the default branch, so the tree is clean and
        the diff empty — the "nothing to publish" short-circuit would return before the owed MR was ever
        retried, leaving the branch MR-less forever while every reply promised otherwise.
        """
        publisher = _make_publisher()
        mr = _make_merge_request(source_branch="daiv/owed-an-mr")
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with (
            self._metadata(publisher),
            patch.object(publisher, "_create_merge_request", return_value=mr) as create,
            patch.object(publisher, "_suggest_context_file"),
        ):
            outcome = await publisher.publish(merge_request=None, pending_branch="daiv/owed-an-mr")

        assert outcome == PublishOutcome(merge_request=mr, published=True)
        assert create.await_args.args[0] == "daiv/owed-an-mr"
        # Nothing new to commit or push — this turn only owes the merge request.
        gm.commit_all.assert_not_called()
        gm.push_head_to.assert_not_called()

    async def test_owed_mr_metadata_comes_from_the_pending_branch(self, monkeypatch):
        """Title/description must describe what is *on the branch*, not the empty working tree."""
        publisher = _make_publisher()
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(publisher, "_diff_to_metadata", return_value=self._metadata_value()) as meta,
            patch.object(publisher, "_create_merge_request", return_value=_make_merge_request()),
            patch.object(publisher, "_suggest_context_file"),
        ):
            await publisher.publish(merge_request=None, pending_branch="daiv/owed-an-mr")

        gm.get_range_diff.assert_awaited_once_with(base_branch="main", head_branch="daiv/owed-an-mr")
        assert meta.await_args.kwargs["pr_metadata_diff"] == "range diff"
        # This path commits nothing, so a commit message must NOT be requested: the two halves are
        # independent agents, and asking for one buys a discarded LLM call over the whole branch diff.
        assert not meta.await_args.kwargs.get("commit_message_diff")
        assert not meta.await_args.args

    async def test_owed_mr_stays_pending_when_the_platform_refuses_again(self, monkeypatch):
        """A retry that hits the same lag must keep owing the *same* branch, not start a new one."""

        publisher = _make_publisher()
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(publisher, "_diff_to_metadata", return_value=self._metadata_value()),
            patch.object(
                publisher,
                "_create_merge_request",
                side_effect=MergeRequestBranchNotVisibleError("daiv/owed-an-mr", verified=True),
            ),
        ):
            outcome = await publisher.publish(merge_request=None, pending_branch="daiv/owed-an-mr")

        assert outcome == PublishOutcome(
            merge_request=None, published=True, pending_branch="daiv/owed-an-mr", pending_branch_verified=True
        )
        gm.unique_branch_name.assert_not_called()

    async def test_voids_the_debt_when_the_owed_branch_is_gone_from_the_remote(self, monkeypatch):
        """A vanished pending branch must void the debt, not crash the turn.

        `get_range_diff` raises when `origin/<branch>` doesn't resolve, and nothing upstream catches it:
        the exception escapes publish, the manager's recovery calls publish again and hits the same
        crash, and because it happens before any state write the pending branch stays checkpointed — so
        EVERY later turn on the thread fails identically and the agent's reply is discarded each time.
        The triggers are ordinary: the user follows the notice's advice and opens the MR themselves, it
        merges, and GitLab deletes the source branch.
        """
        from git import GitCommandError

        publisher = _make_publisher()
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        gm.get_range_diff = AsyncMock(side_effect=GitCommandError("git diff", 128))
        _patch_open_git_manager(monkeypatch, gm)

        with patch.object(publisher, "_diff_to_metadata") as meta:
            outcome = await publisher.publish(merge_request=None, pending_branch="daiv/deleted")

        # published=False is what makes the middleware expire the stale pending state.
        assert outcome == PublishOutcome(merge_request=None, published=False)
        meta.assert_not_called()

    async def test_voids_the_debt_when_the_owed_branch_holds_no_changes(self, monkeypatch):
        """A branch that adds nothing over the base owes no merge request — expire it rather than
        opening an empty MR (which the platform rejects) or keeping the notice forever."""
        publisher = _make_publisher()
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        gm.get_range_diff = AsyncMock(return_value="   \n")
        _patch_open_git_manager(monkeypatch, gm)

        with (
            patch.object(publisher, "_diff_to_metadata") as meta,
            patch.object(publisher, "_create_merge_request") as create,
        ):
            outcome = await publisher.publish(merge_request=None, pending_branch="daiv/empty")

        assert outcome == PublishOutcome(merge_request=None, published=False)
        meta.assert_not_called()
        create.assert_not_called()

    async def test_owed_mr_keeps_the_draft_flag_and_suggests_the_context_file(self, monkeypatch):
        """The retry must behave like the create it stands in for: recovery publishes drafts, and the
        context-file suggestion is pinned on the normal create path for the same reason."""
        publisher = _make_publisher()
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)
        mr = _make_merge_request()

        with (
            patch.object(publisher, "_diff_to_metadata", return_value=self._metadata_value()),
            patch.object(publisher, "_create_merge_request", return_value=mr) as create,
            patch.object(publisher, "_suggest_context_file") as suggest,
        ):
            await publisher.publish(merge_request=None, pending_branch="daiv/owed", as_draft=True)

        assert create.await_args.kwargs["as_draft"] is True
        suggest.assert_awaited_once_with(mr)

    async def test_an_mr_in_hand_wins_over_a_pending_branch_for_the_push_target(self, monkeypatch):
        """Both can be set at once (recovery writes state directly), and the MR must win: pushing the
        run's commits to the pending branch instead would silently stop feeding the in-review MR."""
        publisher = _make_publisher()
        gm = _fake_git_manager()
        _patch_open_git_manager(monkeypatch, gm)

        with self._metadata(publisher):
            await publisher.publish(
                merge_request=_make_merge_request(source_branch="feature"), pending_branch="daiv/owed"
            )

        gm.push_head_to.assert_awaited_once_with("feature", integrate_on_reject=True)

    async def test_nothing_to_publish_still_short_circuits_without_a_pending_branch(self, monkeypatch):
        """The owed-MR path must not weaken the ordinary no-op turn (no metadata call, no push)."""
        publisher = _make_publisher()
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with patch.object(publisher, "_diff_to_metadata") as meta:
            outcome = await publisher.publish(merge_request=None, pending_branch=None)

        assert outcome == PublishOutcome(merge_request=None, published=False)
        meta.assert_not_called()
        gm.get_range_diff.assert_not_called()

    @staticmethod
    def _metadata_value():
        return {
            "pr_metadata": Mock(branch="freshly-generated", title="Title", description="Desc"),
            "commit_message": Mock(commit_message="commit msg"),
        }


class TestPublishDecision:
    """The publish decision (formerly GitMiddleware._is_unpublished) now lives in publish()."""

    async def test_returns_nothing_when_clean_and_no_diff(self, monkeypatch):
        publisher = _make_publisher()
        gm = _fake_git_manager(dirty=False, diff="", has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with patch.object(publisher, "_diff_to_metadata") as meta:
            outcome = await publisher.publish(merge_request=None)

        assert outcome == PublishOutcome(merge_request=None, published=False)
        meta.assert_not_called()
        gm.push_head_to.assert_not_called()

    async def test_confirms_existing_mr_without_republishing_when_pushed(self, monkeypatch):
        publisher = _make_publisher()
        mr = _make_merge_request(source_branch="feat/x", merge_request_id=42)
        gm = _fake_git_manager(dirty=False, diff="diff", remote_branches=["feat/x"], has_unpushed=False)
        _patch_open_git_manager(monkeypatch, gm)

        with patch.object(publisher, "_diff_to_metadata") as meta:
            outcome = await publisher.publish(merge_request=mr)

        assert outcome == PublishOutcome(merge_request=mr, published=False)
        meta.assert_not_called()
        gm.push_head_to.assert_not_called()


def test_create_merge_request_and_suggest_are_async():
    assert inspect.iscoroutinefunction(GitChangePublisher._create_merge_request)
    assert inspect.iscoroutinefunction(GitChangePublisher._suggest_context_file)
