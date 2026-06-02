from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from automation.agent.middlewares.git import GitMiddleware
from codebase.base import Scope
from codebase.utils import GitPushPermissionError


def _make_runtime(*, scope: Scope = Scope.ISSUE) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.scope = scope
    runtime.context.merge_request = None
    runtime.context.repository = Mock(slug="a/b")
    runtime.context.config = Mock(default_branch="main")
    runtime.context.gitrepo = Mock()
    return runtime


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

    async def test_aafter_agent_returns_none_when_nothing_to_publish(self):
        """Clean tree / nothing to publish → the publisher returns None and so does the hook."""
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher = MagicMock()
            publisher.protected_branch_fallback_source = None
            publisher.publish = AsyncMock(return_value=None)
            publisher_cls.return_value = publisher

            result = await middleware.aafter_agent({"merge_request": None}, runtime)

        assert result is None
        publisher.publish.assert_awaited_once()

    async def test_aafter_agent_publishes_changes_and_threads_session_id(self):
        """The publisher commits/pushes and opens/updates the MR; the hook returns it in state
        and threads the sandbox session id so the publisher runs git in the right mode."""
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        new_mr = MagicMock(source_branch="daiv/changes", merge_request_id=11)

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher = MagicMock()
            publisher.protected_branch_fallback_source = None
            publisher.publish = AsyncMock(return_value=new_mr)
            publisher_cls.return_value = publisher

            result = await middleware.aafter_agent({"merge_request": None, "session_id": "sid"}, runtime)

        assert result["merge_request"] is new_mr
        assert result["code_changes"] is True
        publisher.publish.assert_awaited_once()
        assert publisher.publish.await_args.kwargs["session_id"] == "sid"

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
        state_mr = MagicMock(source_branch="feature-x")

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
        stale_state_mr = MagicMock(source_branch="feature-x")

        with (
            patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-y"),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock()) as lookup,
        ):
            result = await middleware.abefore_agent({"merge_request": stale_state_mr}, runtime)

        lookup.assert_not_called()
        assert result["merge_request"] is runtime.context.merge_request

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
        """The publisher writes the original (protected) source branch onto itself
        when it has to swap to a fresh MR. ``aafter_agent`` must thread that value
        through the returned state dict — otherwise the manager-side footer
        rendering pipeline (`_render_protected_branch_footer`) never fires and the
        notice silently drops."""
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        new_mr = MagicMock(source_branch="agent/fresh", merge_request_id=200)

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher_instance = MagicMock()
            publisher_instance.protected_branch_fallback_source = None
            publisher_cls.return_value = publisher_instance

            async def publish_side_effect(**kwargs):
                publisher_instance.protected_branch_fallback_source = "feature"
                return new_mr

            publisher_instance.publish = AsyncMock(side_effect=publish_side_effect)

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
        state_mr = MagicMock(merge_request_id=42, source_branch="feature-x")

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
        state_mr = MagicMock(merge_request_id=42, source_branch="feature-x")

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
        request.state = {"merge_request": MagicMock(merge_request_id=999)}  # stale
        request.system_prompt = ""
        request.override = lambda **kw: MagicMock(system_prompt=kw["system_prompt"])

        with patch("automation.agent.middlewares.git.get_repo_ref", return_value="feature-x"):
            await middleware.awrap_model_call(request, fake_handler)

        assert "merge request #7" in captured["system_prompt"]
        assert "merge request #999" not in captured["system_prompt"]

    async def test_aafter_agent_passes_through_none_when_no_fallback(self):
        """When publish completes without protection fallback, the value stays None
        so a stale signal from a prior turn doesn't render a phantom footer."""
        middleware = GitMiddleware()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        new_mr = MagicMock(source_branch="agent/normal", merge_request_id=201)

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher_instance = MagicMock()
            publisher_instance.protected_branch_fallback_source = None
            publisher_instance.publish = AsyncMock(return_value=new_mr)
            publisher_cls.return_value = publisher_instance

            result = await middleware.aafter_agent({"merge_request": None}, runtime)

        assert result is not None
        assert result["protected_branch_fallback_source"] is None
