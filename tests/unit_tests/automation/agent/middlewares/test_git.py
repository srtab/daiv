from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from git import GitCommandError

from automation.agent.git_manager import GitPushPermissionError, SandboxGitProtocolError
from automation.agent.middlewares.file_system import SandboxFileBackend
from automation.agent.middlewares.git import GitMiddleware
from automation.agent.publishers import PublishOutcome
from codebase.base import MergeRequest, Scope, User


def _fake_open_git_manager(gm, calls: dict):
    """Stand-in for the ``open_git_manager`` seam: records the kwargs it was opened with
    (mode selection itself is pinned in test_git_utils.py) and yields ``gm``."""

    @asynccontextmanager
    async def _open(**kwargs):
        calls.update(kwargs)
        yield gm

    return _open


def _make_runtime(*, scope: Scope = Scope.ISSUE) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.scope = scope
    runtime.context.merge_request = None
    runtime.context.repository = Mock(slug="a/b")
    runtime.context.config = Mock(default_branch="main")
    runtime.context.gitrepo = Mock()
    runtime.context.gitrepo.head.is_detached = False
    return runtime


def _bound_backend() -> SandboxFileBackend:
    """A ``SandboxFileBackend`` already bound to a session, as ``SandboxMiddleware.abefore_agent``
    leaves it on a normal run. The client is a bare sentinel: the publisher is mocked in these
    tests, so no client method is ever called — only ``is_bound()`` is consulted."""
    backend = SandboxFileBackend(client=object())
    backend.bind_session("sid")
    return backend


def _mr(iid=7, branch="feat/y") -> MergeRequest:
    return MergeRequest(
        repo_id="g/r",
        merge_request_id=iid,
        source_branch=branch,
        target_branch="main",
        title="t",
        description="d",
        author=User(id=1, username="daiv"),
        draft=False,
        web_url="u",
    )


def _build_runtime_for_prompt(*, scope: Scope = Scope.GLOBAL) -> Mock:
    """Runtime mock with concrete strings so ``GIT_SYSTEM_PROMPT.format`` can render."""
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.scope = scope
    runtime.context.merge_request = None
    runtime.context.issue = None
    runtime.context.repository = Mock(slug="a/b")
    runtime.context.config = Mock(default_branch="main")
    runtime.context.gitrepo = Mock()
    runtime.context.git_platform = Mock(value="gitlab")
    return runtime


class TestGitMiddleware:
    async def test_aafter_agent_propagates_push_permission_error(self):
        middleware = GitMiddleware()
        runtime = _make_runtime()

        with (
            patch(
                "automation.agent.middlewares.git.GitChangePublisher.publish",
                new=AsyncMock(side_effect=GitPushPermissionError("No permission to push")),
            ),
            pytest.raises(GitPushPermissionError),
        ):
            await middleware.aafter_agent(state={"merge_request": None}, runtime=runtime)

    async def test_aafter_agent_maps_published_outcome_to_state(self):
        """A published outcome maps onto the streamed ``merge_request`` field plus the private
        ``code_changes`` / ``protected_branch_fallback_source`` flags."""
        mw = GitMiddleware(auto_commit_changes=True, sandbox_backend=_bound_backend())
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub = pub_cls.return_value
            pub.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            result = await mw.aafter_agent({"session_id": "s", "merge_request": None}, runtime)
        assert result["merge_request"].merge_request_id == 7
        assert result["code_changes"] is True
        assert result["protected_branch_fallback_source"] is None

    async def test_aafter_agent_returns_none_when_nothing_to_publish(self):
        """A no-op outcome (no MR at all) returns None — nothing to surface in state."""
        mw = GitMiddleware(auto_commit_changes=True, sandbox_backend=_bound_backend())
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=None, published=False))
            assert await mw.aafter_agent({"session_id": "s", "merge_request": None}, runtime) is None

    async def test_aafter_agent_confirms_existing_mr_without_fallback_key(self):
        """A clean tree already on its MR: publish returns the MR with ``published=False``, so
        aafter confirms it in state WITHOUT touching ``protected_branch_fallback_source`` (a
        no-op turn must not clobber a prior turn's fallback signal)."""
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        state_mr = _mr(iid=10, branch="daiv/feature")

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(merge_request=state_mr, published=False)
            )
            result = await middleware.aafter_agent({"merge_request": state_mr}, runtime)

        assert result == {"merge_request": state_mr, "code_changes": True}

    async def test_aafter_agent_passes_backend_to_publisher(self):
        """Daiv publishes via the publisher; the run's bound backend is injected at construction
        (no session_id is threaded through publish)."""
        sentinel_backend = _bound_backend()
        middleware = GitMiddleware(sandbox_backend=sentinel_backend)
        new_mr = _mr()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher = MagicMock()
            publisher.publish = AsyncMock(return_value=PublishOutcome(merge_request=new_mr, published=True))
            publisher_cls.return_value = publisher

            result = await middleware.aafter_agent({"merge_request": None, "session_id": "sid"}, runtime)

        assert publisher_cls.call_args.kwargs["sandbox_backend"] is sentinel_backend
        publisher.publish.assert_awaited_once()
        assert "session_id" not in publisher.publish.await_args.kwargs
        assert result["merge_request"] is new_mr

    async def test_aafter_agent_skips_publish_when_sandbox_unbound(self):
        """Builtin slash commands (``/clear``, ``/help``, ...) jump to the after_agent chain
        from ``SlashCommandMiddleware.abefore_agent``, skipping ``SandboxMiddleware.abefore_agent``
        — so the run's ``SandboxFileBackend`` is never bound and the agent loop never ran. Probing
        git through the unbound backend raised ``SandboxFileBackend is not bound to a sandbox
        session``; aafter_agent must no-op instead (nothing to publish)."""
        backend = SandboxFileBackend(client=object())  # constructed but never bound
        mw = GitMiddleware(auto_commit_changes=True, sandbox_backend=backend)
        runtime = _make_runtime(scope=Scope.GLOBAL)

        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert result is None
        pub_cls.assert_not_called()

    async def test_aafter_agent_skips_patch_capture_when_sandbox_unbound(self):
        """The capture-patch branch also opens a git manager against the sandbox, so the unbound
        short-circuit must skip it too — before the publish gate, so it never touches the backend."""
        backend = SandboxFileBackend(client=object())
        mw = GitMiddleware(auto_commit_changes=False, capture_patch=True, sandbox_backend=backend)
        runtime = _make_runtime(scope=Scope.GLOBAL)

        with patch("automation.agent.middlewares.git.open_git_manager") as open_gm:
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert result is None
        open_gm.assert_not_called()

    async def test_aafter_agent_skips_when_auto_commit_disabled(self):
        middleware = GitMiddleware(auto_commit_changes=False)
        runtime = _make_runtime()

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            result = await middleware.aafter_agent({"merge_request": None}, runtime)

        assert result is None
        publisher_cls.assert_not_called()

    async def test_abefore_agent_seeds_open_mr_when_state_empty(self):
        """No MR in state → seed it so it streams via STATE_SNAPSHOT."""
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        existing_mr = MagicMock(source_branch="feature-x")

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch(
                "automation.agent.middlewares.git.GitMiddleware._alookup_open_mr",
                new=AsyncMock(return_value=existing_mr),
            ) as lookup,
        ):
            result = await middleware.abefore_agent({}, runtime)

        lookup.assert_awaited_once_with(runtime.context)
        # `protected_branch_fallback_source` is reset to None so a stale signal from
        # a prior checkpointed turn cannot bleed into this run's reply rendering.
        assert result == {"merge_request": existing_mr, "code_changes": False, "protected_branch_fallback_source": None}

    async def test_abefore_agent_skips_lookup_when_state_has_mr(self):
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        state_mr = _mr(branch="feature-x")

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock()) as lookup,
        ):
            result = await middleware.abefore_agent({"merge_request": state_mr}, runtime)

        lookup.assert_not_called()
        assert result["merge_request"] is state_mr

    async def test_abefore_agent_runtime_context_overrides_state_in_mr_scope(self):
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.MERGE_REQUEST)
        runtime.context.merge_request = MagicMock(source_branch="feature-y")
        stale_state_mr = _mr(branch="feature-x")

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-y"),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock()) as lookup,
        ):
            result = await middleware.abefore_agent({"merge_request": stale_state_mr}, runtime)

        lookup.assert_not_called()
        assert result["merge_request"] is runtime.context.merge_request

    async def test_abefore_agent_surfaces_dirty_workspace_on_capture_patch(self, caplog):
        """Pre-run dirt vs HEAD ends up verbatim inside the captured model_patch — flag it
        loudly AND machine-readably (state + run tree), but never abort the run over a
        diagnostic."""
        middleware = GitMiddleware(auto_commit_changes=False, capture_patch=True)
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = AsyncMock()
        gm.get_changed_files.return_value = ["tests/demo/symlink.txt", "my file.txt"]
        run_tree = MagicMock(metadata={})

        with (
            patch("automation.agent.middlewares.git.open_git_manager", new=_fake_open_git_manager(gm, {})),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.git.get_current_run_tree", return_value=run_tree),
            caplog.at_level("ERROR"),
        ):
            result = await middleware.abefore_agent({}, runtime)

        assert "dirty" in caplog.text
        # Names come from `git diff --name-only`, not header parsing — exact even with spaces.
        assert "tests/demo/symlink.txt" in caplog.text
        assert "my file.txt" in caplog.text
        assert result["pre_run_dirty_files"] == ["tests/demo/symlink.txt", "my file.txt"]
        assert run_tree.metadata["pre_run_dirty_files"] == ["tests/demo/symlink.txt", "my file.txt"]

    async def test_abefore_agent_dirty_log_truncates_long_file_lists(self, caplog):
        """The ERROR log samples the first 20 names with an ellipsis; the state field keeps
        the full list."""
        middleware = GitMiddleware(auto_commit_changes=False, capture_patch=True)
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = AsyncMock()
        gm.get_changed_files.return_value = [f"f{i}.py" for i in range(21)]

        with (
            patch("automation.agent.middlewares.git.open_git_manager", new=_fake_open_git_manager(gm, {})),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.git.get_current_run_tree", return_value=None),
            caplog.at_level("ERROR"),
        ):
            result = await middleware.abefore_agent({}, runtime)

        assert "21 file(s)" in caplog.text
        assert "f19.py, …" in caplog.text
        assert "f20.py" not in caplog.text
        assert len(result["pre_run_dirty_files"]) == 21

    async def test_abefore_agent_clean_workspace_logs_nothing(self, caplog):
        middleware = GitMiddleware(auto_commit_changes=False, capture_patch=True)
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = AsyncMock()
        gm.get_changed_files.return_value = []

        with (
            patch("automation.agent.middlewares.git.open_git_manager", new=_fake_open_git_manager(gm, {})),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=None)),
            caplog.at_level("ERROR"),
        ):
            result = await middleware.abefore_agent({}, runtime)

        assert "dirty" not in caplog.text
        assert "pre_run_dirty_files" not in result

    async def test_abefore_agent_skips_dirty_check_when_capture_disabled(self):
        """Normal (non-eval) runs must not pay the extra git RPC."""
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)

        with (
            patch("automation.agent.middlewares.git.open_git_manager") as open_gm,
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=None)),
        ):
            result = await middleware.abefore_agent({}, runtime)

        open_gm.assert_not_called()
        assert "pre_run_dirty_files" not in result

    @pytest.mark.parametrize(
        "fault", [GitCommandError("git diff", 128), SandboxGitProtocolError("Sandbox returned no result")]
    )
    async def test_abefore_agent_dirty_check_failure_is_non_fatal(self, caplog, fault):
        """The check is diagnostic only: a git/wire fault must be logged, not raised."""
        middleware = GitMiddleware(auto_commit_changes=False, capture_patch=True)
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = AsyncMock()
        gm.get_changed_files.side_effect = fault

        with (
            patch("automation.agent.middlewares.git.open_git_manager", new=_fake_open_git_manager(gm, {})),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=None)),
            caplog.at_level("ERROR"),
        ):
            result = await middleware.abefore_agent({}, runtime)

        assert "dirty-tree check failed" in caplog.text
        assert result is not None

    async def test_abefore_agent_dirty_check_propagates_wiring_bugs(self):
        """The non-fatal catch is deliberately narrow: a bare RuntimeError signals a
        programming error (mode mismatch, asyncio misuse) and must fail the run loudly,
        not degrade into a silently skipped check."""
        middleware = GitMiddleware(auto_commit_changes=False, capture_patch=True)
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = AsyncMock()
        gm.get_changed_files.side_effect = RuntimeError("GitManager is not in sandbox mode")

        with (
            patch("automation.agent.middlewares.git.open_git_manager", new=_fake_open_git_manager(gm, {})),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=None)),
            pytest.raises(RuntimeError, match="not in sandbox mode"),
        ):
            await middleware.abefore_agent({}, runtime)

    async def test_alookup_open_mr_returns_platform_mr(self):
        runtime = _make_runtime()
        existing_mr = MagicMock(source_branch="feature-x")
        client = MagicMock()
        client.get_merge_request_by_branches = MagicMock(return_value=existing_mr)

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch("automation.agent.middlewares.git.RepoClient.create_instance", return_value=client),
        ):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is existing_mr
        client.get_merge_request_by_branches.assert_called_once_with("a/b", "feature-x", "main")

    async def test_alookup_open_mr_skips_detached_head(self):
        """Commit-pinned runs (SWE-bench evals check out a raw SHA) have no branch, so
        no MR can exist — the lookup must short-circuit before any platform call."""
        runtime = _make_runtime()
        runtime.context.gitrepo.head.is_detached = True

        with (
            patch(
                "automation.agent.middlewares.git.get_repo_ref", return_value="80e486c6dce6d10b13ef1705a8e9255bbc4a521b"
            ),
            patch("automation.agent.middlewares.git.RepoClient.create_instance") as create,
        ):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is None
        create.assert_not_called()

    async def test_alookup_open_mr_uses_run_platform_client(self):
        """The lookup must query the run's platform, not the settings default — a run
        whose repo lives elsewhere would otherwise 404 against the configured platform
        or, worse, match a same-named repo's MR."""
        runtime = _make_runtime()
        client = MagicMock()
        client.get_merge_request_by_branches = MagicMock(return_value=None)

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch("automation.agent.middlewares.git.RepoClient.create_instance", return_value=client) as create,
        ):
            await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        create.assert_called_once_with(git_platform=runtime.context.git_platform)

    async def test_alookup_open_mr_skips_default_branch(self):
        runtime = _make_runtime()

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="main"),
            patch("automation.agent.middlewares.git.RepoClient.create_instance") as create,
        ):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is None
        create.assert_not_called()

    async def test_alookup_open_mr_swallows_platform_errors(self):
        from gitlab.exceptions import GitlabError

        runtime = _make_runtime()
        client = MagicMock()
        client.get_merge_request_by_branches = MagicMock(side_effect=GitlabError("gitlab down"))

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch("automation.agent.middlewares.git.RepoClient.create_instance", return_value=client),
        ):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is None

    async def test_alookup_open_mr_swallows_transport_errors(self):
        """The platform SDKs (python-gitlab, PyGithub) are requests-based: a network blip
        surfaces as a requests exception, not a Gitlab/Github API error. This lookup is
        best-effort — a transient outage must degrade to "no MR", not crash the run."""
        import requests

        runtime = _make_runtime()
        client = MagicMock()
        client.get_merge_request_by_branches = MagicMock(side_effect=requests.ConnectionError("dns failure"))

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch("automation.agent.middlewares.git.RepoClient.create_instance", return_value=client),
        ):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is None

    async def test_alookup_open_mr_propagates_unexpected_errors(self):
        """Bugs (KeyError/AttributeError) must NOT be silently caught — the
        publisher would then create a duplicate MR thinking none existed.
        """
        runtime = _make_runtime()
        client = MagicMock()
        client.get_merge_request_by_branches = MagicMock(side_effect=KeyError("missing field"))

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"),
            patch("automation.agent.middlewares.git.RepoClient.create_instance", return_value=client),
            pytest.raises(KeyError),
        ):
            await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

    async def test_aafter_agent_returns_protected_branch_fallback_source(self):
        """The publisher reports the original (protected) source branch on the returned
        ``PublishOutcome`` when it has to swap to a fresh MR. ``aafter_agent`` must thread that value
        through the returned state dict — otherwise the manager-side footer rendering pipeline
        (`_render_protected_branch_footer`) never fires and the notice silently drops."""
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        new_mr = _mr(iid=200, branch="agent/fresh")

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(
                    merge_request=new_mr, published=True, protected_branch_fallback_source="feature"
                )
            )

            result = await middleware.aafter_agent({"merge_request": None}, runtime)

        assert result is not None
        assert result["merge_request"] is new_mr
        assert result["code_changes"] is True
        assert result["protected_branch_fallback_source"] == "feature"

    async def test_awrap_model_call_uses_state_mr_when_context_mr_missing(self):
        """Chat triggers run with ``scope=Global`` and ``context.merge_request=None``
        even when the current branch has an open MR. The id discovered by
        ``abefore_agent`` (stored in state) must surface in the system prompt;
        otherwise the agent re-discovers via ``project-merge-request list`` every
        turn and may pick a different MR than the publisher will write to."""
        middleware = GitMiddleware()
        runtime = _build_runtime_for_prompt(scope=Scope.GLOBAL)
        state_mr = _mr(iid=42, branch="feature-x")

        captured = {}

        async def fake_handler(req):
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        request = MagicMock()
        request.runtime = runtime
        request.state = {"merge_request": state_mr}
        request.system_prompt = ""
        request.override = lambda **kw: MagicMock(system_prompt=kw["system_prompt"])

        with patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"):
            await middleware.awrap_model_call(request, fake_handler)

        assert "merge request #42" in captured["system_prompt"]

    async def test_awrap_model_call_drops_state_mr_when_branch_diverged(self):
        """If the working tree was checked out off the source branch between
        ``abefore_agent`` and this hop, the state MR is stale — don't advertise
        it to the model on subsequent turns."""
        middleware = GitMiddleware()
        runtime = _build_runtime_for_prompt(scope=Scope.GLOBAL)
        state_mr = _mr(iid=42, branch="feature-x")

        captured = {}

        async def fake_handler(req):
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        request = MagicMock()
        request.runtime = runtime
        request.state = {"merge_request": state_mr}
        request.system_prompt = ""
        request.override = lambda **kw: MagicMock(system_prompt=kw["system_prompt"])

        # Current ref now differs from the state MR's source_branch.
        with patch("automation.agent.middlewares.git.get_repo_ref", return_value="other-branch"):
            await middleware.awrap_model_call(request, fake_handler)

        assert "merge request #42" not in captured["system_prompt"]

    async def test_awrap_model_call_no_mr_at_all_omits_section(self):
        """Chat default: no context MR, no state MR. The ``{{#merge_request_iid}}``
        block must not render — otherwise the prompt could leak a ``merge request
        #None`` artefact on a future template tweak."""
        middleware = GitMiddleware()
        runtime = _build_runtime_for_prompt(scope=Scope.GLOBAL)

        captured = {}

        async def fake_handler(req):
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        request = MagicMock()
        request.runtime = runtime
        request.state = {}
        request.system_prompt = ""
        request.override = lambda **kw: MagicMock(system_prompt=kw["system_prompt"])

        with patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"):
            await middleware.awrap_model_call(request, fake_handler)

        assert "merge request #" not in captured["system_prompt"]

    async def _render_git_prompt(self, middleware) -> str:
        runtime = _build_runtime_for_prompt(scope=Scope.GLOBAL)
        captured = {}

        async def fake_handler(req):
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        request = MagicMock()
        request.runtime = runtime
        request.state = {}
        request.system_prompt = ""
        request.override = lambda **kw: MagicMock(system_prompt=kw["system_prompt"])

        with patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"):
            await middleware.awrap_model_call(request, fake_handler)
        return captured["system_prompt"]

    async def test_prompt_states_committing_is_automatic(self):
        """The git prompt tells the agent committing/pushing is handled by the harness."""
        prompt = await self._render_git_prompt(GitMiddleware())

        assert "Committing and pushing is automatic" in prompt

    async def test_awrap_model_call_prefers_context_mr_over_state(self):
        """When ``context.merge_request`` is set (true MR-scoped trigger), it
        wins — the publisher and the prompt agree on the same id."""
        middleware = GitMiddleware()
        runtime = _build_runtime_for_prompt(scope=Scope.MERGE_REQUEST)
        runtime.context.merge_request = MagicMock(merge_request_id=7)

        captured = {}

        async def fake_handler(req):
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        request = MagicMock()
        request.runtime = runtime
        request.state = {"merge_request": _mr(iid=999, branch="feature-x")}  # stale
        request.system_prompt = ""
        request.override = lambda **kw: MagicMock(system_prompt=kw["system_prompt"])

        with patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"):
            await middleware.awrap_model_call(request, fake_handler)

        assert "merge request #7" in captured["system_prompt"]
        assert "merge request #999" not in captured["system_prompt"]

    async def test_aafter_agent_captures_patch_in_sandbox_mode(self):
        """``capture_patch=True`` + a bound backend: the diff is taken through the run's
        mode-matched git handle (``open_git_manager`` receives the backend — git runs in the
        authoritative /workspace/repo) and surfaced in state."""
        sentinel_backend = _bound_backend()
        mw = GitMiddleware(auto_commit_changes=False, capture_patch=True, sandbox_backend=sentinel_backend)
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = MagicMock(get_diff=AsyncMock(return_value="diff --git a/x b/x\n"))
        calls = {}

        with patch("automation.agent.middlewares.git.open_git_manager", new=_fake_open_git_manager(gm, calls)):
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert calls == {"sandbox_backend": sentinel_backend, "gitrepo": runtime.context.gitrepo}
        assert result == {"model_patch": "diff --git a/x b/x\n"}

    async def test_aafter_agent_captures_patch_in_local_mode(self):
        """No sandbox backend (disk-backed run): ``open_git_manager`` gets ``sandbox_backend=None``
        and falls back to the local clone."""
        mw = GitMiddleware(auto_commit_changes=False, capture_patch=True)
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = MagicMock(get_diff=AsyncMock(return_value="local diff\n"))
        calls = {}

        with patch("automation.agent.middlewares.git.open_git_manager", new=_fake_open_git_manager(gm, calls)):
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert calls == {"sandbox_backend": None, "gitrepo": runtime.context.gitrepo}
        assert result == {"model_patch": "local diff\n"}

    async def test_aafter_agent_no_patch_key_when_capture_disabled(self):
        """Default (capture off): normal runs never carry a patch through state — the key would
        otherwise stream in AG-UI STATE_SNAPSHOT events."""
        mw = GitMiddleware(auto_commit_changes=False)
        runtime = _make_runtime(scope=Scope.GLOBAL)

        with patch("automation.agent.middlewares.git.open_git_manager") as open_gm:
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        open_gm.assert_not_called()
        assert result is None

    async def test_aafter_agent_captures_patch_before_publish(self):
        """With both flags on, capture must run BEFORE publish — the publisher's commit moves
        HEAD, which would empty a diff-vs-HEAD taken afterwards."""
        mw = GitMiddleware(auto_commit_changes=True, capture_patch=True, sandbox_backend=_bound_backend())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        order: list[str] = []

        async def fake_get_diff(*args, **kwargs):  # noqa: ARG001
            order.append("capture")
            return "patch\n"

        async def fake_publish(*args, **kwargs):  # noqa: ARG001
            order.append("publish")
            return PublishOutcome(merge_request=_mr(), published=True)

        with (
            patch(
                "automation.agent.middlewares.git.open_git_manager",
                new=_fake_open_git_manager(MagicMock(get_diff=fake_get_diff), {}),
            ),
            patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls,
        ):
            pub_cls.return_value.publish = fake_publish
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert order == ["capture", "publish"]
        assert result["model_patch"] == "patch\n"
        assert result["merge_request"].merge_request_id == 7
        assert result["code_changes"] is True

    async def test_aafter_agent_propagates_capture_failure_when_not_publishing(self):
        """Eval path (auto_commit off): the patch IS the run's artifact — a capture failure must
        fail loudly, not degrade to an empty patch indistinguishable from "agent made no
        changes" (the exact failure mode capture_patch exists to fix)."""
        mw = GitMiddleware(auto_commit_changes=False, capture_patch=True, sandbox_backend=_bound_backend())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = MagicMock(get_diff=AsyncMock(side_effect=GitCommandError(["git", "diff"], 128, "boom")))

        with (
            patch("automation.agent.middlewares.git.open_git_manager", new=_fake_open_git_manager(gm, {})),
            patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls,
            pytest.raises(GitCommandError),
        ):
            await mw.aafter_agent({"merge_request": None}, runtime)

        pub_cls.assert_not_called()

    async def test_aafter_agent_capture_failure_does_not_block_publish(self):
        """Capture is read-only observability; when the turn publishes, a capture failure must
        not abort the publish — the agent's work would be stranded uncommitted in the sandbox."""
        mw = GitMiddleware(auto_commit_changes=True, capture_patch=True, sandbox_backend=_bound_backend())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = MagicMock(get_diff=AsyncMock(side_effect=GitCommandError(["git", "diff"], 128, "boom")))

        with (
            patch("automation.agent.middlewares.git.open_git_manager", new=_fake_open_git_manager(gm, {})),
            patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls,
        ):
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert result["merge_request"].merge_request_id == 7
        assert "model_patch" not in result

    async def test_aafter_agent_capture_wiring_bug_propagates_even_when_publishing(self):
        """The capture catch is deliberately narrow: a bare RuntimeError is a programming
        error (mode mismatch, asyncio misuse), not a degradable git fault — it must fail
        the run loudly instead of being masked as "publishing without model_patch"."""
        mw = GitMiddleware(auto_commit_changes=True, capture_patch=True, sandbox_backend=_bound_backend())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = MagicMock(get_diff=AsyncMock(side_effect=RuntimeError("GitManager is not in sandbox mode")))

        with (
            patch("automation.agent.middlewares.git.open_git_manager", new=_fake_open_git_manager(gm, {})),
            patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls,
            pytest.raises(RuntimeError, match="not in sandbox mode"),
        ):
            await mw.aafter_agent({"merge_request": None}, runtime)

        pub_cls.assert_not_called()

    async def test_aafter_agent_passes_through_none_when_no_fallback(self):
        """When publish completes without protection fallback, the value stays None
        so a stale signal from a prior turn doesn't render a phantom footer."""
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        new_mr = _mr(iid=201, branch="agent/normal")

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(merge_request=new_mr, published=True)
            )

            result = await middleware.aafter_agent({"merge_request": None}, runtime)

        assert result is not None
        assert result["protected_branch_fallback_source"] is None


def test_effective_mr_iid_prefers_context_mr():
    assert GitMiddleware._effective_mr_iid(context_mr=_mr(1, "a"), state_mr=_mr(2, "b"), current_ref="b") == 1


def test_effective_mr_iid_uses_state_mr_when_branch_matches():
    assert GitMiddleware._effective_mr_iid(context_mr=None, state_mr=_mr(2, "feat/x"), current_ref="feat/x") == 2


def test_effective_mr_iid_drops_stale_state_mr():
    assert GitMiddleware._effective_mr_iid(context_mr=None, state_mr=_mr(2, "feat/x"), current_ref="main") is None


def test_effective_mr_iid_none_when_no_mr():
    assert GitMiddleware._effective_mr_iid(context_mr=None, state_mr=None, current_ref="main") is None


def test_state_merge_request_returns_model_when_valid():
    mr = _mr()
    assert GitMiddleware._state_merge_request({"merge_request": mr}) is mr


def test_state_merge_request_returns_none_when_absent():
    assert GitMiddleware._state_merge_request({}) is None
    assert GitMiddleware._state_merge_request({"merge_request": None}) is None


def test_state_merge_request_raises_on_degraded_dict(caplog):
    # A checkpointed MergeRequest that failed to revive comes back as the raw lc:2 envelope dict
    # (langgraph-checkpoint-redis swallows the reconstruction error). Fail loud at the read site
    # instead of letting `.source_branch` raise a confusing AttributeError far downstream.
    envelope = {"lc": 2, "type": "constructor", "id": ["codebase.base", "MergeRequest"], "kwargs": {}}
    with caplog.at_level("ERROR"), pytest.raises(TypeError, match="expected MergeRequest"):
        GitMiddleware._state_merge_request({"merge_request": envelope})
    assert "did not revive to a MergeRequest" in caplog.text
