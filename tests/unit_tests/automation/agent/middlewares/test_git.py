from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from git import GitCommandError
from langchain_core.messages import AIMessage
from langchain_core.prompts import SystemMessagePromptTemplate

from automation.agent.context import RuntimeCtx
from automation.agent.events import ASSISTANT_MESSAGE_EVENT, PUBLISH_RESULT_EVENT, publish_result_payload
from automation.agent.git_state import RepoPublish
from automation.agent.middlewares.git import GIT_SYSTEM_PROMPT, GitMiddleware
from automation.agent.publishers import PublishOutcome, UnresolvedMergeConflictsError
from automation.agent.synthetic import is_synthetic
from automation.agent.workspace.git_manager import GitPushPermissionError
from automation.agent.workspace.git_runners import SandboxGitProtocolError
from codebase.base import GitPlatform, MergeRequest, MergeRequestDiffStats, Scope, User
from codebase.clients.checkout import CheckoutReport
from codebase.repo_config import Checkout
from tests.unit_tests.automation.agent.conftest import make_repo_handle
from tests.unit_tests.conftest import FakeWorkspace, agent_settings

SETTINGS = agent_settings()


def _make_runtime(*, scope: Scope = Scope.ISSUE) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.scope = scope
    runtime.context.merge_request = None
    runtime.context.repository = Mock(slug="a/b")
    runtime.context.config = Mock(default_branch="main", checkout=Checkout())
    runtime.context.repo.repository = runtime.context.repository
    runtime.context.repo.repo_id = "a/b"
    runtime.context.repo.config = runtime.context.config
    runtime.context.repo.head_detached = False
    runtime.context.repo.submodules = ()
    runtime.context.repo.checkout = CheckoutReport.EMPTY
    runtime.context.extra_repos = ()
    runtime.context.repos = (runtime.context.repo,)
    return runtime


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
    runtime.context.config = Mock(default_branch="main", checkout=Checkout())
    runtime.context.git_platform = Mock(value="gitlab")
    runtime.context.extra_repos = ()
    runtime.context.repo.checkout = CheckoutReport.EMPTY
    runtime.context.repo.submodules = ()
    runtime.context.repos = (runtime.context.repo,)
    return runtime


@pytest.fixture(autouse=True)
def dispatched():
    with patch("automation.agent.middlewares.git.adispatch_custom_event", new=AsyncMock()) as dispatch:
        yield dispatch


class TestPublishResult:
    @staticmethod
    async def _after_agent(*, outcome=None, raises=None):
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=outcome, side_effect=raises)
            return await mw.aafter_agent({"merge_request": None}, runtime)

    @pytest.mark.parametrize(
        ("outcome", "status", "error_kind"),
        [
            (PublishOutcome(merge_request=_mr(), published=True, created_merge_request=True), "opened", None),
            (PublishOutcome(merge_request=_mr(), published=True), "pushed", None),
            (PublishOutcome(merge_request=None, published=True), "pushed", "mr_pending"),
        ],
        ids=["opened", "pushed", "mr-pending"],
    )
    async def test_the_status_follows_what_the_publish_did(self, dispatched, outcome, status, error_kind):
        await self._after_agent(outcome=outcome)

        dispatched.assert_awaited_once()
        name, payload = dispatched.await_args.args
        assert (name, payload["status"], payload["error_kind"]) == (PUBLISH_RESULT_EVENT, status, error_kind)

    async def test_the_result_carries_the_merge_request_and_diff_stats(self, dispatched):
        stats = MergeRequestDiffStats(files_changed=2, lines_added=5, lines_removed=1)
        outcome = PublishOutcome(merge_request=_mr(), published=True, created_merge_request=True, diff_stats=stats)

        await self._after_agent(outcome=outcome)

        dispatched.assert_awaited_once_with(
            PUBLISH_RESULT_EVENT,
            publish_result_payload(status="opened", merge_request=_mr(), diff_stats=stats, repository="a/b"),
        )

    async def test_a_turn_with_nothing_to_publish_dispatches_no_result(self, dispatched):
        await self._after_agent(outcome=PublishOutcome(merge_request=_mr(), published=False))

        dispatched.assert_not_awaited()

    @pytest.mark.parametrize(
        "kwargs",
        [{"workspace": FakeWorkspace(is_ready=False)}, {"workspace": FakeWorkspace(), "auto_commit_changes": False}],
        ids=["workspace-not-ready", "auto-commit-off"],
    )
    async def test_a_turn_end_that_skips_the_publish_dispatches_no_result(self, dispatched, kwargs):
        await GitMiddleware(settings=SETTINGS, **kwargs).aafter_agent({}, _make_runtime(scope=Scope.GLOBAL))

        dispatched.assert_not_awaited()

    async def test_conflicts_are_not_published(self, dispatched):
        await self._after_agent(raises=UnresolvedMergeConflictsError(["a.py", "b.py"]))

        dispatched.assert_awaited_once_with(
            PUBLISH_RESULT_EVENT,
            publish_result_payload(status="not_published", error_kind="conflicts", repository="a/b"),
        )

    @pytest.mark.parametrize(
        ("failure", "kind"), [(GitPushPermissionError("denied"), "push_rejected"), (RuntimeError("boom"), "unknown")]
    )
    async def test_a_failure_is_emitted_as_failed_then_raised_as_is(self, dispatched, failure, kind):
        with pytest.raises(type(failure)) as info:
            await self._after_agent(raises=failure)

        assert info.value is failure
        dispatched.assert_awaited_once_with(
            PUBLISH_RESULT_EVENT, publish_result_payload(status="failed", error_kind=kind, repository="a/b")
        )

    async def test_a_failed_dispatch_does_not_fail_the_turn(self, dispatched):
        dispatched.side_effect = RuntimeError("no parent run")

        update = await self._after_agent(outcome=PublishOutcome(merge_request=_mr(), published=True))

        assert update["published"] is True


class TestGitMiddleware:
    async def test_aafter_agent_propagates_push_permission_error(self):
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime()

        with (
            patch(
                "automation.agent.middlewares.git.GitChangePublisher.publish",
                new=AsyncMock(side_effect=GitPushPermissionError("No permission to push")),
            ),
            pytest.raises(GitPushPermissionError),
        ):
            await middleware.aafter_agent(state={"merge_request": None}, runtime=runtime)

    async def test_a_merge_left_with_conflict_markers_ends_the_turn_with_a_reply_naming_the_files(self):
        """Not a run failure: a failure would surface as a generic error and drop the agent's turn."""
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(side_effect=UnresolvedMergeConflictsError(["a.py", "b.py"]))
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        [reply] = result["messages"]
        assert isinstance(reply, AIMessage)
        assert "`a.py`, `b.py` still have conflict markers" in reply.content
        assert "published" not in result

    async def test_aafter_agent_maps_published_outcome_to_state(self):
        """A published outcome maps onto the streamed ``merge_request`` field plus the private
        ``code_changes`` / ``protected_branch_fallback_source`` flags."""
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub = pub_cls.return_value
            pub.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            result = await mw.aafter_agent({"session_id": "s", "merge_request": None}, runtime)
        assert result["merge_request"].merge_request_id == 7
        assert result["code_changes"] is True
        assert result["protected_branch_fallback_source"] is None

    @pytest.mark.parametrize("session_links", [AsyncMock(), None], ids=["linked", "unlinked"])
    async def test_aafter_agent_passes_session_links_to_publisher(self, session_links):
        """A run without links (one-shot, or links off) still publishes, just without them."""
        mw = GitMiddleware(
            settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace(), session_links=session_links
        )
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            await mw.aafter_agent({"session_id": "s", "merge_request": None}, runtime)
        assert pub_cls.call_args.kwargs["session_links"] is session_links

    async def test_aafter_agent_hands_the_publisher_the_runs_settings(self):
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            await mw.aafter_agent({"session_id": "s", "merge_request": None}, runtime)
        assert pub_cls.call_args.kwargs["settings"] is SETTINGS

    async def test_aafter_agent_surfaces_diff_stats_as_a_plain_dict(self):
        """``diff_stats`` streams to the chat composer through the same STATE_SNAPSHOT the MR
        rides on, so it is dumped to plain ints rather than checkpointed as a model."""
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(
                    merge_request=_mr(),
                    published=True,
                    diff_stats=MergeRequestDiffStats(lines_added=182, lines_removed=41, files_changed=6),
                )
            )
            result = await mw.aafter_agent({"session_id": "s", "merge_request": None}, runtime)
        assert result["diff_stats"] == {"lines_added": 182, "lines_removed": 41, "files_changed": 6}

    async def test_aafter_agent_omits_diff_stats_when_there_are_none(self):
        """A turn that published nothing leaves the key out entirely, so the composer keeps
        showing counts instead of a stale line total."""
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            result = await mw.aafter_agent({"session_id": "s", "merge_request": None}, runtime)
        assert "diff_stats" not in result

    @pytest.mark.parametrize(
        ("stats", "expected"),
        [
            pytest.param(None, None, id="nothing-measured"),
            # A turn that reverts its predecessor's work publishes zeros, and those have to reach
            # state or the earlier turn's numbers stand after a reload.
            pytest.param(
                MergeRequestDiffStats(),
                {"diff_stats": {"lines_added": 0, "lines_removed": 0, "files_changed": 0}},
                id="measured-zero",
            ),
        ],
    )
    async def test_aafter_agent_surfaces_a_no_op_publish_only_when_it_measured(self, stats, expected):
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        runtime.context.extra_repos = ()
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(merge_request=None, published=False, diff_stats=stats)
            )
            result = await mw.aafter_agent({"session_id": "s", "merge_request": None}, runtime)
        assert result == expected

    async def test_aafter_agent_records_code_changes_on_pending_mr_degrade(self):
        """The branch-visibility degrade (published + no MR) must flip ``code_changes`` so a pushed
        branch is not persisted as an indistinguishable no-op."""
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        runtime.context.extra_repos = ()
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=None, published=True))
            result = await mw.aafter_agent({"session_id": "s", "merge_request": None}, runtime)
        assert result == {"code_changes": True, "published": True}

    async def test_aafter_agent_confirms_existing_mr_without_fallback_key(self):
        """A clean tree already on its MR: publish returns the MR with ``published=False``, so
        aafter confirms it in state WITHOUT touching ``protected_branch_fallback_source`` (a
        no-op turn must not clobber a prior turn's fallback signal)."""
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        state_mr = _mr(iid=10, branch="daiv/feature")

        with (
            patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls,
            patch.object(runtime.context.repo, "current_ref", "daiv/feature"),
        ):
            publisher_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(merge_request=state_mr, published=False)
            )
            result = await middleware.aafter_agent({"merge_request": state_mr}, runtime)

        assert result == {"merge_request": state_mr, "code_changes": True}

    @pytest.mark.parametrize(
        ("scope", "state_branch", "current_ref", "keeps_mr", "levels"),
        [
            # An issue-scope follow-up turn re-clones the default branch while the checkpoint still
            # names the first turn's MR. Publishing there pushes a sibling of that branch's tip: the
            # push is rejected, the rebase conflicts, and the turn's work is discarded. DAIV chose
            # that ref itself (``Session.ref``), so the drop is its own continuity failing — ERROR is
            # the only level this project turns into a Sentry event.
            (Scope.ISSUE, "fix/10-update-dependencies", "master", False, ["ERROR"]),
            # A chat turn may legitimately retarget the composer's branch pill mid-session and land
            # on the same guard, so paging outside issue scope would train the signal away.
            (Scope.GLOBAL, "feat/x", "feat/other", False, ["WARNING"]),
            # The guard must not cost the normal case: still on the MR's branch, still one MR.
            (Scope.GLOBAL, "feat/x", "feat/x", True, []),
        ],
    )
    async def test_aafter_agent_publish_target_follows_the_checked_out_ref(
        self, scope, state_branch, current_ref, keeps_mr, levels, caplog
    ):
        """The publish target is dropped on the same terms the prompt's MR iid already is."""
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=scope)
        state_mr = _mr(iid=449, branch=state_branch)

        with (
            patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls,
            patch.object(runtime.context.repo, "current_ref", current_ref),
            caplog.at_level("WARNING"),
        ):
            publisher = MagicMock()
            publisher.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(iid=450), published=True))
            publisher_cls.return_value = publisher
            await middleware.aafter_agent({"merge_request": state_mr}, runtime)

        assert publisher.publish.await_args.kwargs["merge_request"] is (state_mr if keeps_mr else None)
        assert [r.levelname for r in caplog.records] == levels
        if not keeps_mr:
            # The drop changes the run's outcome (a fresh MR instead of an update), so it must be
            # traceable to the MR and the ref that disagreed.
            assert all(token in caplog.text for token in ("449", state_branch, current_ref))

    async def test_aafter_agent_publishes_onto_the_context_mr_on_an_mr_scope_run(self):
        """An MR-scope run's context MR is authoritative — it is the ref the clone was made on.
        A commit-pinned clone records a SHA as its ref, which matches no branch name,
        so the guard must defer to the context MR instead of dropping it."""
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.MERGE_REQUEST)
        context_mr = _mr(iid=77, branch="feat/z")
        runtime.context.merge_request = context_mr

        with (
            patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls,
            patch.object(runtime.context.repo, "current_ref", "deadbeef"),
        ):
            publisher = MagicMock()
            publisher.publish = AsyncMock(return_value=PublishOutcome(merge_request=context_mr, published=True))
            publisher_cls.return_value = publisher
            await middleware.aafter_agent({"merge_request": context_mr}, runtime)

        assert publisher.publish.await_args.kwargs["merge_request"] is context_mr

    async def test_aafter_agent_marks_a_real_publish_as_published(self):
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            result = await mw.aafter_agent({"session_id": "s", "merge_request": None}, runtime)
        assert result["published"] is True

    async def test_aafter_agent_does_not_mark_a_no_op_turn_as_published(self):
        """``code_changes`` stays true for a clean tree already on its MR — the dashboard and MCP
        count the run's work — so it cannot also mean "this turn pushed". A fix run is *always*
        already on its MR, which made the watch's no-diff give-up unreachable.
        """
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        state_mr = _mr(iid=10, branch="daiv/feature")
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(merge_request=state_mr, published=False)
            )
            result = await mw.aafter_agent({"merge_request": state_mr}, runtime)
        assert result["code_changes"] is True
        assert result.get("published") is not True

    async def test_abefore_agent_clears_published_each_turn(self):
        """``published`` is per-turn, and ``aafter_agent`` only ever sets it true — so the reset
        here is what stops turn 2 inheriting turn 1's push."""
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        with patch.object(GitMiddleware, "_alookup_open_mr", AsyncMock(return_value=None)):
            result = await middleware.abefore_agent({}, runtime)
        assert result["published"] is False

    async def test_repo_publishes_reset_each_turn(self):
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        state = {
            "repo_publishes": [{"repo_id": "g/mod", "error": "Publishing to g/mod failed (RuntimeError)"}],
            "main_publish_error": "Publishing to g/app failed (RuntimeError)",
        }
        with patch.object(GitMiddleware, "_alookup_open_mr", AsyncMock(return_value=None)):
            result = await middleware.abefore_agent(state, runtime)
        assert result["repo_publishes"] == []
        assert result["main_publish_error"] is None

    async def test_aafter_agent_passes_the_workspace_to_publisher(self):
        """The publisher publishes through the run's workspace, handed over at construction (no session id is threaded
        through publish)."""
        workspace = FakeWorkspace()
        middleware = GitMiddleware(settings=SETTINGS, workspace=workspace)
        new_mr = _mr()
        runtime = _make_runtime(scope=Scope.GLOBAL)
        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher = MagicMock()
            publisher.publish = AsyncMock(return_value=PublishOutcome(merge_request=new_mr, published=True))
            publisher_cls.return_value = publisher

            result = await middleware.aafter_agent({"merge_request": None, "session_id": "sid"}, runtime)

        assert publisher_cls.call_args.args == (runtime.context, workspace)
        publisher.publish.assert_awaited_once()
        assert "session_id" not in publisher.publish.await_args.kwargs
        assert result["merge_request"] is new_mr

    async def test_aafter_agent_skips_publish_when_the_workspace_is_not_ready(self):
        """Builtin slash commands (``/clear``, ``/help``, ...) jump to the after_agent chain from
        ``SlashCommandMiddleware.abefore_agent``, skipping ``SandboxMiddleware.abefore_agent`` — so a sandbox run's
        session is never acquired and the agent loop never ran. Probing git through that workspace would raise
        (``SandboxFileBackend is not bound to a sandbox session``); aafter_agent must no-op instead."""
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace(is_ready=False), auto_commit_changes=True)
        runtime = _make_runtime(scope=Scope.GLOBAL)

        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert result == {"code_changes": False, "published": False, "repo_publishes": [], "main_publish_error": None}
        pub_cls.assert_not_called()

    async def test_a_short_circuited_turn_clears_the_previous_turns_publish_results(self):
        """The slash command's jump also skips ``abefore_agent``, the only other reset: without this the executor
        would report, persist and re-arm the CI watch of the previous turn's publishes."""
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace(is_ready=False), auto_commit_changes=True)
        previous = {
            "published": True,
            "code_changes": True,
            "repo_publishes": [{"repo_id": "g/mod", "published": True}],
            "main_publish_error": "Publishing to g/app failed (RuntimeError)",
        }

        result = await mw.aafter_agent({"merge_request": None, **previous}, _make_runtime(scope=Scope.GLOBAL))

        assert result == {"code_changes": False, "published": False, "repo_publishes": [], "main_publish_error": None}

    async def test_aafter_agent_skips_patch_capture_when_the_workspace_is_not_ready(self):
        """The capture-patch branch runs git too, so the not-ready short-circuit must skip it — before the publish
        gate, so it never touches the workspace's git."""
        workspace = FakeWorkspace(is_ready=False)
        mw = GitMiddleware(settings=SETTINGS, workspace=workspace, auto_commit_changes=False, capture_patch=True)

        result = await mw.aafter_agent({"merge_request": None}, _make_runtime(scope=Scope.GLOBAL))

        assert result == {"code_changes": False, "published": False, "repo_publishes": [], "main_publish_error": None}
        assert workspace.git.mock_calls == []

    async def test_aafter_agent_skips_when_auto_commit_disabled(self):
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace(), auto_commit_changes=False)
        runtime = _make_runtime()

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            result = await middleware.aafter_agent({"merge_request": None}, runtime)

        assert result is None
        publisher_cls.assert_not_called()

    async def test_abefore_agent_seeds_open_mr_when_state_empty(self):
        """No MR in state → seed it so it streams via STATE_SNAPSHOT."""
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        existing_mr = MagicMock(source_branch="feature-x")

        with (
            patch.object(runtime.context.repo, "current_ref", "feature-x"),
            patch(
                "automation.agent.middlewares.git.GitMiddleware._alookup_open_mr",
                new=AsyncMock(return_value=existing_mr),
            ) as lookup,
        ):
            result = await middleware.abefore_agent({}, runtime)

        lookup.assert_awaited_once_with(runtime.context)
        # `protected_branch_fallback_source` is reset to None so a stale signal from
        # a prior checkpointed turn cannot bleed into this run's reply rendering.
        assert result == {
            "merge_request": existing_mr,
            "code_changes": False,
            "published": False,
            "protected_branch_fallback_source": None,
            "repo_publishes": [],
            "main_publish_error": None,
        }

    async def test_a_forked_run_leaves_the_checked_out_branchs_open_mr_alone(self):
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace(), fork_branch=True)
        runtime = _make_runtime(scope=Scope.GLOBAL)

        with (
            patch.object(runtime.context.repo, "current_ref", "feature-x"),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock()) as lookup,
        ):
            result = await middleware.abefore_agent({}, runtime)

        lookup.assert_not_called()
        assert result["merge_request"] is None

    async def test_abefore_agent_skips_lookup_when_state_has_mr(self):
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        state_mr = _mr(branch="feature-x")

        with (
            patch.object(runtime.context.repo, "current_ref", "feature-x"),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock()) as lookup,
        ):
            result = await middleware.abefore_agent({"merge_request": state_mr}, runtime)

        lookup.assert_not_called()
        assert result["merge_request"] is state_mr

    async def test_a_checkout_of_another_branch_finds_that_branchs_open_mr(self):
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        other_mr = _mr(branch="feature-y")
        open_mr = MagicMock(source_branch="feature-x")

        with (
            patch.object(runtime.context.repo, "current_ref", "feature-x"),
            patch(
                "automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=open_mr)
            ),
        ):
            result = await middleware.abefore_agent({"merge_request": other_mr}, runtime)

        assert result["merge_request"] is open_mr

    async def test_abefore_agent_runtime_context_overrides_state_in_mr_scope(self):
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.MERGE_REQUEST)
        runtime.context.merge_request = MagicMock(source_branch="feature-y")
        stale_state_mr = _mr(branch="feature-x")

        with (
            patch.object(runtime.context.repo, "current_ref", "feature-y"),
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock()) as lookup,
        ):
            result = await middleware.abefore_agent({"merge_request": stale_state_mr}, runtime)

        lookup.assert_not_called()
        assert result["merge_request"] is runtime.context.merge_request

    async def test_abefore_agent_surfaces_dirty_workspace_on_capture_patch(self, caplog):
        """Pre-run dirt vs HEAD ends up verbatim inside the captured model_patch — flag it
        loudly AND machine-readably (state + run tree), but never abort the run over a
        diagnostic."""
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = AsyncMock()
        gm.get_changed_files.return_value = ["tests/demo/symlink.txt", "my file.txt"]
        run_tree = MagicMock(metadata={})

        middleware = GitMiddleware(
            settings=SETTINGS, workspace=FakeWorkspace(git=gm), auto_commit_changes=False, capture_patch=True
        )
        with (
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
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = AsyncMock()
        gm.get_changed_files.return_value = [f"f{i}.py" for i in range(21)]

        middleware = GitMiddleware(
            settings=SETTINGS, workspace=FakeWorkspace(git=gm), auto_commit_changes=False, capture_patch=True
        )
        with (
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
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = AsyncMock()
        gm.get_changed_files.return_value = []

        middleware = GitMiddleware(
            settings=SETTINGS, workspace=FakeWorkspace(git=gm), auto_commit_changes=False, capture_patch=True
        )
        with (
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=None)),
            caplog.at_level("ERROR"),
        ):
            result = await middleware.abefore_agent({}, runtime)

        assert "dirty" not in caplog.text
        assert "pre_run_dirty_files" not in result

    async def test_abefore_agent_skips_dirty_check_when_capture_disabled(self):
        """Normal (non-eval) runs must not pay the extra git RPC."""
        workspace = FakeWorkspace()
        middleware = GitMiddleware(settings=SETTINGS, workspace=workspace)
        runtime = _make_runtime(scope=Scope.GLOBAL)

        with patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=None)):
            result = await middleware.abefore_agent({}, runtime)

        assert workspace.git.mock_calls == []
        assert "pre_run_dirty_files" not in result

    @pytest.mark.parametrize(
        "fault", [GitCommandError("git diff", 128), SandboxGitProtocolError("Sandbox returned no result")]
    )
    async def test_abefore_agent_dirty_check_failure_is_non_fatal(self, caplog, fault):
        """The check is diagnostic only: a git/wire fault must be logged, not raised."""
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = AsyncMock()
        gm.get_changed_files.side_effect = fault

        middleware = GitMiddleware(
            settings=SETTINGS, workspace=FakeWorkspace(git=gm), auto_commit_changes=False, capture_patch=True
        )
        with (
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=None)),
            caplog.at_level("ERROR"),
        ):
            result = await middleware.abefore_agent({}, runtime)

        assert "dirty-tree check failed" in caplog.text
        assert result is not None

    async def test_abefore_agent_dirty_check_propagates_wiring_bugs(self):
        """The non-fatal catch is deliberately narrow: a bare RuntimeError signals a
        programming error (unbound-session guard, asyncio misuse) and must fail the run loudly,
        not degrade into a silently skipped check."""
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = AsyncMock()
        gm.get_changed_files.side_effect = RuntimeError("SandboxFileBackend is not bound to a sandbox session")

        middleware = GitMiddleware(
            settings=SETTINGS, workspace=FakeWorkspace(git=gm), auto_commit_changes=False, capture_patch=True
        )
        with (
            patch("automation.agent.middlewares.git.GitMiddleware._alookup_open_mr", new=AsyncMock(return_value=None)),
            pytest.raises(RuntimeError, match="not bound to a sandbox session"),
        ):
            await middleware.abefore_agent({}, runtime)

    async def test_alookup_open_mr_returns_platform_mr(self):
        runtime = _make_runtime()
        existing_mr = MagicMock(source_branch="feature-x")
        runtime.context.repo.client.get_merge_request_by_branches.return_value = existing_mr

        with patch.object(runtime.context.repo, "current_ref", "feature-x"):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is existing_mr
        runtime.context.repo.client.get_merge_request_by_branches.assert_called_once_with("a/b", "feature-x")

    async def test_alookup_open_mr_returns_nondefault_target_mr(self):
        """The lookup no longer filters on target, so an MR targeting a release branch is found."""
        runtime = _make_runtime()
        existing_mr = MagicMock(source_branch="feature-x", target_branch="release/x")
        runtime.context.repo.client.get_merge_request_by_branches.return_value = existing_mr

        with patch.object(runtime.context.repo, "current_ref", "feature-x"):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr.target_branch == "release/x"

    async def test_alookup_open_mr_skips_detached_head(self):
        """Commit-pinned runs (SWE-bench evals check out a raw SHA) have no branch, so
        no MR can exist — the lookup must short-circuit before any platform call."""
        runtime = _make_runtime()
        runtime.context.repo.head_detached = True

        with patch.object(runtime.context.repo, "current_ref", "80e486c6dce6d10b13ef1705a8e9255bbc4a521b"):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is None
        runtime.context.repo.client.get_merge_request_by_branches.assert_not_called()

    async def test_alookup_open_mr_skips_default_branch(self):
        runtime = _make_runtime()

        with patch.object(runtime.context.repo, "current_ref", "main"):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is None
        runtime.context.repo.client.get_merge_request_by_branches.assert_not_called()

    async def test_alookup_open_mr_soft_fails_on_platform_errors(self):
        from gitlab.exceptions import GitlabError

        runtime = _make_runtime()
        runtime.context.repo.client.get_merge_request_by_branches.side_effect = GitlabError("gitlab down")

        with patch.object(runtime.context.repo, "current_ref", "feature-x"):
            mr = await GitMiddleware._alookup_open_mr(runtime.context)  # noqa: SLF001

        assert mr is None

    async def test_aafter_agent_returns_protected_branch_fallback_source(self):
        """The publisher reports the original (protected) source branch on the returned
        ``PublishOutcome`` when it has to swap to a fresh MR. ``aafter_agent`` must thread that value
        through the returned state dict — otherwise the manager-side footer rendering pipeline
        (`_render_protected_branch_footer`) never fires and the notice silently drops."""
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
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
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
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

        with patch.object(runtime.context.repo, "current_ref", "feature-x"):
            await middleware.awrap_model_call(request, fake_handler)

        assert "merge request #42" in captured["system_prompt"]

    async def test_awrap_model_call_drops_state_mr_when_branch_diverged(self):
        """If the working tree was checked out off the source branch between
        ``abefore_agent`` and this hop, the state MR is stale — don't advertise
        it to the model on subsequent turns."""
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
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
        with patch.object(runtime.context.repo, "current_ref", "other-branch"):
            await middleware.awrap_model_call(request, fake_handler)

        assert "merge request #42" not in captured["system_prompt"]

    async def test_awrap_model_call_no_mr_at_all_omits_section(self):
        """Chat default: no context MR, no state MR. The ``{{#merge_request_iid}}``
        block must not render — otherwise the prompt could leak a ``merge request
        #None`` artefact on a future template tweak."""
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
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

        with patch.object(runtime.context.repo, "current_ref", "feature-x"):
            await middleware.awrap_model_call(request, fake_handler)

        assert "merge request #" not in captured["system_prompt"]

    async def _render_git_prompt(self, middleware, runtime=None) -> str:
        runtime = runtime or _build_runtime_for_prompt(scope=Scope.GLOBAL)
        captured = {}

        async def fake_handler(req):
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        request = MagicMock()
        request.runtime = runtime
        request.state = {}
        request.system_prompt = ""
        request.override = lambda **kw: MagicMock(system_prompt=kw["system_prompt"])

        with patch.object(runtime.context.repo, "current_ref", "feature-x"):
            await middleware.awrap_model_call(request, fake_handler)
        return captured["system_prompt"]

    async def test_prompt_states_committing_is_automatic(self):
        """The git prompt tells the agent committing/pushing is handled by the harness."""
        prompt = await self._render_git_prompt(GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace()))

        assert "Committing and pushing is automatic" in prompt

    async def test_prompt_lists_other_repositories(self):
        runtime = _build_runtime_for_prompt(scope=Scope.GLOBAL)
        runtime.context.extra_repos = (
            SimpleNamespace(
                repository=SimpleNamespace(slug="g/mod"),
                current_ref="fix-x",
                config=SimpleNamespace(default_branch="main"),
                workspace_path="/workspace/repos/g/mod",
            ),
        )

        prompt = await self._render_git_prompt(GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace()), runtime)

        assert "## Other repositories in this session" in prompt
        assert "`g/mod` at `/workspace/repos/g/mod` (current branch: `fix-x`, default branch: `main`)" in prompt
        assert "git -C <path>" in prompt

    async def test_single_repo_prompt_has_no_other_repositories_section(self):
        prompt = await self._render_git_prompt(GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace()))

        assert "Other repositories in this session" not in prompt

    async def test_awrap_model_call_prefers_context_mr_over_state(self):
        """When ``context.merge_request`` is set (true MR-scoped trigger), it
        wins — the publisher and the prompt agree on the same id."""
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
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

        with patch.object(runtime.context.repo, "current_ref", "feature-x"):
            await middleware.awrap_model_call(request, fake_handler)

        assert "merge request #7" in captured["system_prompt"]
        assert "merge request #999" not in captured["system_prompt"]

    async def test_aafter_agent_captures_patch_through_the_workspace_git(self):
        """``capture_patch=True``: the diff is taken by the workspace's git, where the agent's changes are, and
        surfaced in state."""
        workspace = FakeWorkspace(git=MagicMock(get_diff=AsyncMock(return_value="diff --git a/x b/x\n")))
        mw = GitMiddleware(settings=SETTINGS, workspace=workspace, auto_commit_changes=False, capture_patch=True)

        result = await mw.aafter_agent({"merge_request": None}, _make_runtime(scope=Scope.GLOBAL))

        workspace.git.get_diff.assert_awaited_once_with()
        assert result == {"model_patch": "diff --git a/x b/x\n"}

    async def test_a_turn_never_reads_the_worker_clone(self):
        """A turn reaches git only through the workspace, never ``context.gitrepo``: the pre-run check, the MR lookup,
        the prompt, the patch capture and the publish target read the ref recorded at clone time."""
        workspace = FakeWorkspace(
            git=MagicMock(get_changed_files=AsyncMock(return_value=[]), get_diff=AsyncMock(return_value=""))
        )
        mw = GitMiddleware(settings=SETTINGS, workspace=workspace, auto_commit_changes=True, capture_patch=True)
        repository, config = Mock(slug="a/b"), Mock(default_branch="main", checkout=Checkout())
        repo = SimpleNamespace(
            repository=repository,
            config=config,
            current_ref="feature-x",
            head_detached=False,
            client=MagicMock(get_merge_request_by_branches=MagicMock(return_value=None)),
            checkout=CheckoutReport.EMPTY,
            submodules=(),
        )
        runtime = Mock(
            context=SimpleNamespace(
                scope=Scope.GLOBAL,
                merge_request=None,
                issue=None,
                repository=repository,
                config=config,
                git_platform=Mock(value="gitlab"),
                extra_repos=(),
                repo=repo,
                repos=(repo,),
            )
        )
        request = MagicMock(runtime=runtime, state={"merge_request": None}, system_prompt="")
        request.override = lambda **kw: MagicMock(system_prompt=kw["system_prompt"])

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(merge_request=None, published=False)
            )
            await mw.abefore_agent({}, runtime)
            await mw.awrap_model_call(request, AsyncMock())
            await mw.aafter_agent({"merge_request": None}, runtime)

        workspace.git.get_changed_files.assert_awaited_once_with()
        workspace.git.get_diff.assert_awaited_once_with()
        runtime.context.repo.client.get_merge_request_by_branches.assert_called_once_with("a/b", "feature-x")
        assert publisher_cls.call_args.args == (runtime.context, workspace)
        assert publisher_cls.return_value.publish.await_args.kwargs["merge_request"] is None

    async def test_aafter_agent_no_patch_key_when_capture_disabled(self):
        """Default (capture off): normal runs never carry a patch through state — the key would
        otherwise stream in AG-UI STATE_SNAPSHOT events."""
        workspace = FakeWorkspace()
        mw = GitMiddleware(settings=SETTINGS, workspace=workspace, auto_commit_changes=False)
        runtime = _make_runtime(scope=Scope.GLOBAL)

        result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert workspace.git.mock_calls == []
        assert result is None

    async def test_aafter_agent_captures_patch_before_publish(self):
        """With both flags on, capture must run BEFORE publish — the publisher's commit moves
        HEAD, which would empty a diff-vs-HEAD taken afterwards."""
        runtime = _make_runtime(scope=Scope.GLOBAL)
        order: list[str] = []

        async def fake_get_diff(*args, **kwargs):  # noqa: ARG001
            order.append("capture")
            return "patch\n"

        async def fake_publish(*args, **kwargs):  # noqa: ARG001
            order.append("publish")
            return PublishOutcome(merge_request=_mr(), published=True)

        mw = GitMiddleware(
            settings=SETTINGS,
            auto_commit_changes=True,
            capture_patch=True,
            workspace=FakeWorkspace(git=MagicMock(get_diff=fake_get_diff)),
        )
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
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
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = MagicMock(get_diff=AsyncMock(side_effect=GitCommandError(["git", "diff"], 128, "boom")))

        mw = GitMiddleware(
            settings=SETTINGS, auto_commit_changes=False, capture_patch=True, workspace=FakeWorkspace(git=gm)
        )
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls, pytest.raises(GitCommandError):
            await mw.aafter_agent({"merge_request": None}, runtime)

        pub_cls.assert_not_called()

    async def test_aafter_agent_capture_failure_does_not_block_publish(self):
        """Capture is read-only observability; when the turn publishes, a capture failure must
        not abort the publish — the agent's work would be stranded uncommitted in the sandbox."""
        runtime = _make_runtime(scope=Scope.GLOBAL)
        gm = MagicMock(get_diff=AsyncMock(side_effect=GitCommandError(["git", "diff"], 128, "boom")))

        mw = GitMiddleware(
            settings=SETTINGS, auto_commit_changes=True, capture_patch=True, workspace=FakeWorkspace(git=gm)
        )
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert result["merge_request"].merge_request_id == 7
        assert "model_patch" not in result

    async def test_aafter_agent_capture_wiring_bug_propagates_even_when_publishing(self):
        """The capture catch is deliberately narrow: a bare RuntimeError is a programming
        error (unbound-session guard, asyncio misuse), not a degradable git fault — it must fail
        the run loudly instead of being masked as "publishing without model_patch"."""
        runtime = _make_runtime(scope=Scope.GLOBAL)
        unbound = RuntimeError("SandboxFileBackend is not bound to a sandbox session")
        gm = MagicMock(get_diff=AsyncMock(side_effect=unbound))

        mw = GitMiddleware(
            settings=SETTINGS, auto_commit_changes=True, capture_patch=True, workspace=FakeWorkspace(git=gm)
        )
        with (
            patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls,
            pytest.raises(RuntimeError, match="not bound to a sandbox session"),
        ):
            await mw.aafter_agent({"merge_request": None}, runtime)

        pub_cls.assert_not_called()

    async def test_aafter_agent_passes_through_none_when_no_fallback(self):
        """When publish completes without protection fallback, the value stays None
        so a stale signal from a prior turn doesn't render a phantom footer."""
        middleware = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _make_runtime(scope=Scope.GLOBAL)
        new_mr = _mr(iid=201, branch="agent/normal")

        with patch("automation.agent.middlewares.git.GitChangePublisher") as publisher_cls:
            publisher_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(merge_request=new_mr, published=True)
            )

            result = await middleware.aafter_agent({"merge_request": None}, runtime)

        assert result is not None
        assert result["protected_branch_fallback_source"] is None


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
    assert "revived as dict, not a MergeRequest" in caplog.text


class TestAgentSummaryReachesThePublisher:
    """The transcript is only readable here — ``sessions.executor.recovery.recover_draft`` publishes from a
    persisted checkpoint, where ``messages`` lives in a ``DeltaChannel`` and needs
    ``aresolve_thread_messages`` to reconstruct. So this hook is the one path that can supply it."""

    @staticmethod
    async def _summary_handed_to_publish(state):
        mw = GitMiddleware(settings=SETTINGS, auto_commit_changes=True, workspace=FakeWorkspace())
        runtime = MagicMock()
        runtime.context.scope = Scope.GLOBAL
        runtime.context.extra_repos = ()
        with patch("automation.agent.middlewares.git.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            await mw.aafter_agent(state, runtime)
            return pub_cls.return_value.publish.await_args.kwargs["agent_summary"]

    async def test_the_closing_summary_is_handed_to_publish(self):
        summary = await self._summary_handed_to_publish({
            "merge_request": None,
            "messages": [AIMessage("Added the endpoint; could not run the suite.")],
        })

        assert summary == "Added the endpoint; could not run the suite."

    async def test_a_transcript_without_prose_publishes_without_a_summary(self):
        summary = await self._summary_handed_to_publish({
            "merge_request": None,
            "messages": [AIMessage("", tool_calls=[{"name": "bash", "args": {}, "id": "c1"}])],
        })

        assert summary is None

    async def test_a_state_carrying_no_messages_publishes_without_a_summary(self):
        """A builtin slash command short-circuits straight to the after_agent chain."""
        summary = await self._summary_handed_to_publish({"merge_request": None})

        assert summary is None

    async def test_an_unreadable_transcript_still_publishes(self, caplog):
        """The read is evaluated as an argument to publish, so anything it raises means nothing is
        committed, pushed or opened — over a sentence in a description."""
        with caplog.at_level("ERROR"):
            summary = await self._summary_handed_to_publish({"merge_request": None, "messages": object()})

        assert summary is None
        assert "closing summary" in caplog.text


def _multi_repo_runtime(*extra_repos) -> Mock:
    return Mock(context=RuntimeCtx(bot_username="daiv", repos=(make_repo_handle("g/app"), *extra_repos)))


_SIBLING_ERROR = RepoPublish(
    repo_id="g/mod",
    merge_request=None,
    published=False,
    current_ref="main",
    error="Publishing to g/mod failed (RuntimeError)",
    error_kind="unknown",
)


class TestExtraRepoPublish:
    async def test_the_siblings_publish_after_the_main_repository(self):
        """The turn-end claim is what marks a sibling's push as DAIV's, so the siblings push last."""
        order: list[str] = []
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())

        with (
            patch("automation.agent.publishers.GitChangePublisher") as sibling_cls,
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
        ):
            sibling_cls.return_value.publish = AsyncMock(
                side_effect=lambda **_: order.append("g/mod") or PublishOutcome(merge_request=None, published=False)
            )
            main_cls.return_value.publish = AsyncMock(
                side_effect=lambda **_: order.append("g/app") or PublishOutcome(merge_request=_mr(), published=True)
            )
            await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime(make_repo_handle("g/mod")))

        assert order == ["g/app", "g/mod"]

    async def test_sibling_failure_is_recorded_and_main_still_publishes(self, caplog):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())

        with (
            patch("automation.agent.publishers.GitChangePublisher") as sibling_cls,
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
            caplog.at_level("ERROR"),
        ):
            sibling_cls.return_value.publish = AsyncMock(side_effect=RuntimeError("push rejected"))
            main_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            result = await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime(make_repo_handle("g/mod")))

        main_cls.return_value.publish.assert_awaited_once()
        assert result["merge_request"] == _mr()
        assert result["repo_publishes"] == [_SIBLING_ERROR.to_state()]
        assert "push rejected" in caplog.text

    async def test_a_failed_main_publish_keeps_the_sibling_publishes(self, caplog):
        """Raising would discard this node's writes, and with them the siblings' freshly opened merge requests."""
        workspace = FakeWorkspace()
        links = AsyncMock()
        mw = GitMiddleware(settings=SETTINGS, workspace=workspace, skip_ci=True, session_links=links)
        runtime = _multi_repo_runtime(make_repo_handle("g/mod"))

        with (
            patch(
                "automation.agent.middlewares.git.apublish_extra_repos", AsyncMock(return_value=[_SIBLING_ERROR])
            ) as publish,
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
            caplog.at_level("ERROR"),
        ):
            main_cls.return_value.publish = AsyncMock(side_effect=RuntimeError("platform 500"))
            result = await mw.aafter_agent({"merge_request": None, "messages": [AIMessage("Fixed both.")]}, runtime)

        publish.assert_awaited_once_with(
            runtime.context, workspace, settings=SETTINGS, session_links=links, skip_ci=True
        )
        assert result["repo_publishes"] == [_SIBLING_ERROR.to_state()]
        assert result["main_publish_error"] == "Publishing to g/app failed (RuntimeError)"
        notice, failures = result["messages"]
        assert "main repository `g/app`" in notice.content
        assert not is_synthetic(notice)
        assert is_synthetic(failures)
        assert f"- `g/mod`: {_SIBLING_ERROR.error}" in failures.content
        assert "platform 500" in caplog.text

    async def test_a_failed_main_publish_still_raises_without_extra_repositories(self):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())

        with (
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
            pytest.raises(RuntimeError, match="platform 500"),
        ):
            main_cls.return_value.publish = AsyncMock(side_effect=RuntimeError("platform 500"))
            await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime())

    async def test_a_main_conflict_keeps_the_sibling_publishes(self):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())

        with (
            patch("automation.agent.middlewares.git.apublish_extra_repos", AsyncMock(return_value=[_SIBLING_ERROR])),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
        ):
            main_cls.return_value.publish = AsyncMock(side_effect=UnresolvedMergeConflictsError(["a.py"]))
            result = await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime(make_repo_handle("g/mod")))

        assert result["messages"][0].content.startswith(
            "I didn't publish the merge in the main repository `g/app`: `a.py` still has conflict markers, "
            "so nothing was committed or pushed there."
        )
        assert result["repo_publishes"] == [_SIBLING_ERROR.to_state()]
        assert result["main_publish_error"] == str(UnresolvedMergeConflictsError(["a.py"]))

    async def test_every_repository_dispatches_its_own_result_in_order(self, dispatched):
        opened = RepoPublish(
            repo_id="g/mod", merge_request=_mr(), published=True, current_ref="fix", created_merge_request=True
        )
        pushed = RepoPublish(repo_id="g/stack", merge_request=_mr(), published=True, current_ref="fix")
        idle = RepoPublish(repo_id="g/idle", merge_request=None, published=False, current_ref="main")
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _multi_repo_runtime(
            make_repo_handle("g/mod"), make_repo_handle("g/stack"), make_repo_handle("g/idle")
        )

        with (
            patch(
                "automation.agent.middlewares.git.apublish_extra_repos", AsyncMock(return_value=[opened, pushed, idle])
            ),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
        ):
            main_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(merge_request=_mr(), published=True, created_merge_request=True)
            )
            await mw.aafter_agent({"merge_request": None}, runtime)

        payloads = [call.args[1] for call in dispatched.await_args_list if call.args[0] == PUBLISH_RESULT_EVENT]
        assert [p["repository"] for p in payloads] == [runtime.context.repo.repo_id, "g/mod", "g/stack"]
        assert [bool(p.get("sibling")) for p in payloads] == [False, True, True]

    async def test_a_failed_main_publish_names_the_main_repository_in_its_result(self, dispatched):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = _multi_repo_runtime(make_repo_handle("g/mod"))

        with (
            patch("automation.agent.middlewares.git.apublish_extra_repos", AsyncMock(return_value=[_SIBLING_ERROR])),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
        ):
            main_cls.return_value.publish = AsyncMock(side_effect=RuntimeError("boom"))
            await mw.aafter_agent({"merge_request": None}, runtime)

        [payload, _sibling] = [call.args[1] for call in dispatched.await_args_list]
        assert payload["repository"] == runtime.context.repo.repo_id

    async def test_the_main_result_waits_for_the_extra_repositories_to_publish(self, dispatched):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())

        async def _publish_extras(*_args, **_kwargs):
            dispatched.assert_not_awaited()
            return [_SIBLING_ERROR]

        with (
            patch("automation.agent.middlewares.git.apublish_extra_repos", AsyncMock(side_effect=_publish_extras)),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
        ):
            main_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(merge_request=_mr(), published=True, created_merge_request=True)
            )
            await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime(make_repo_handle("g/mod")))

        assert dispatched.await_count == 2

    async def test_a_sibling_failure_list_is_synthetic_and_never_streamed(self):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())

        with (
            patch("automation.agent.middlewares.git.apublish_extra_repos", AsyncMock(return_value=[_SIBLING_ERROR])),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
            patch("automation.agent.utils.adispatch_custom_event", new=AsyncMock()) as streamed,
        ):
            main_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=None, published=False))
            result = await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime(make_repo_handle("g/mod")))

        [notice] = result["messages"]
        assert is_synthetic(notice)
        assert "g/mod" in notice.content
        assert [c for c in streamed.await_args_list if c.args[0] == ASSISTANT_MESSAGE_EVENT] == []

    async def test_a_sibling_conflict_keeps_a_visible_notice_naming_the_files(self):
        conflict = RepoPublish(
            repo_id="g/mod",
            merge_request=None,
            published=False,
            current_ref="main",
            error="conflicts",
            error_kind="conflicts",
            conflicted_paths=("a.py",),
        )
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())

        with (
            patch("automation.agent.middlewares.git.apublish_extra_repos", AsyncMock(return_value=[conflict])),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
            patch("automation.agent.utils.adispatch_custom_event", new=AsyncMock()) as streamed,
        ):
            main_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=None, published=False))
            result = await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime(make_repo_handle("g/mod")))

        [notice] = result["messages"]
        assert not is_synthetic(notice)
        [message_call] = [c for c in streamed.await_args_list if c.args[0] == ASSISTANT_MESSAGE_EVENT]
        assert message_call.args[1]["message"] == notice.content
        assert "`a.py`" in notice.content
        assert "in `g/mod`" in notice.content
        assert "Ask me to finish resolving them." in notice.content

    async def test_a_turn_with_nothing_to_publish_still_records_the_siblings(self):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())

        with (
            patch("automation.agent.middlewares.git.apublish_extra_repos", AsyncMock(return_value=[_SIBLING_ERROR])),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
        ):
            main_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=None, published=False))
            result = await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime(make_repo_handle("g/mod")))

        assert result["repo_publishes"] == [_SIBLING_ERROR.to_state()]
        [notice] = result["messages"]
        assert notice.content == (
            f"I couldn't publish the changes in these repositories:\n- `g/mod`: {_SIBLING_ERROR.error}"
        )

    async def test_a_turn_whose_siblings_all_published_adds_no_notice(self):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        published = RepoPublish(repo_id="g/mod", merge_request=None, published=True, current_ref="fix")

        with (
            patch("automation.agent.middlewares.git.apublish_extra_repos", AsyncMock(return_value=[published])),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
        ):
            main_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=None, published=False))
            result = await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime(make_repo_handle("g/mod")))

        assert result == {"repo_publishes": [published.to_state()], "main_publish_error": None}

    async def test_the_main_publish_of_a_multi_repo_turn_gets_no_closing_summary(self):
        """The agent's closing summary covers every repository's work, so the main MR would describe the others'."""
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        state = {"merge_request": None, "messages": [AIMessage("Fixed g/mod; tests in g/lib did not run.")]}
        published = RepoPublish(repo_id="g/mod", merge_request=None, published=True, current_ref="fix")

        with (
            patch("automation.agent.middlewares.git.apublish_extra_repos", AsyncMock(return_value=[published])),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
        ):
            main_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=None, published=False))
            await mw.aafter_agent(state, _multi_repo_runtime(make_repo_handle("g/mod")))

        assert main_cls.return_value.publish.await_args.kwargs["agent_summary"] is None

    async def test_no_repository_publishes_when_auto_commit_is_off(self):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace(), auto_commit_changes=False)

        with patch("automation.agent.middlewares.git.apublish_extra_repos", AsyncMock()) as publish:
            result = await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime(make_repo_handle("g/mod")))

        assert result is None
        publish.assert_not_awaited()

    async def test_a_single_repo_run_records_no_repo_publishes(self):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())

        with (
            patch("automation.agent.middlewares.git.apublish_extra_repos", AsyncMock()) as publish,
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
        ):
            main_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            result = await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime())

        publish.assert_not_awaited()
        assert "repo_publishes" not in result


class TestRelatedLinks:
    @staticmethod
    def _sibling_publish() -> RepoPublish:
        return RepoPublish(repo_id="g/mod", merge_request=_mr(iid=9), published=True, current_ref="feat/y")

    async def _run(self, state, main_publish, runtime):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        with (
            patch(
                "automation.agent.middlewares.git.apublish_extra_repos",
                AsyncMock(return_value=[self._sibling_publish()]),
            ),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
            patch("automation.agent.middlewares.git.aensure_related_links", AsyncMock()) as ensure,
        ):
            main_cls.return_value.publish = main_publish
            await mw.aafter_agent(state, runtime)
        return ensure

    async def test_after_agent_links_main_and_sibling_mrs(self):
        runtime = _multi_repo_runtime(make_repo_handle("g/mod"))
        publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(iid=3), published=True))

        ensure = await self._run({"merge_request": None}, publish, runtime)

        [entries] = ensure.await_args.args
        assert [(e.repo_slug, e.merge_request.merge_request_id) for e in entries] == [("g/app", 3), ("g/mod", 9)]
        assert entries[0].client is runtime.context.repo.client
        assert entries[1].client is runtime.context.extra_repos[0].client

    async def test_a_failed_main_publish_still_links_siblings_to_the_existing_main_mr(self):
        runtime = _multi_repo_runtime(make_repo_handle("g/mod"))

        ensure = await self._run({"merge_request": _mr(iid=3)}, AsyncMock(side_effect=RuntimeError("500")), runtime)

        [entries] = ensure.await_args.args
        assert [e.repo_slug for e in entries] == ["g/app", "g/mod"]

    async def test_a_main_conflict_links_the_siblings_alone_when_there_is_no_main_mr(self):
        runtime = _multi_repo_runtime(make_repo_handle("g/mod"))

        ensure = await self._run(
            {"merge_request": None}, AsyncMock(side_effect=UnresolvedMergeConflictsError(["a.py"])), runtime
        )

        [entries] = ensure.await_args.args
        assert [e.repo_slug for e in entries] == ["g/mod"]

    async def test_a_single_repo_run_does_not_link(self):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        with (
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
            patch("automation.agent.middlewares.git.aensure_related_links", AsyncMock()) as ensure,
        ):
            main_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            await mw.aafter_agent({"merge_request": None}, _multi_repo_runtime())

        ensure.assert_not_awaited()


# ---------------------------------------------------------------------------
# Submodules and LFS: the prompt section, the publish order and the turn-end notice
# ---------------------------------------------------------------------------


def _with_checkout(handle, *, submodules=(), skipped=(), lfs_files=(), lfs=True, sub_on=True):
    from codebase.clients.checkout import CheckoutOptions, CheckoutReport

    report = CheckoutReport(
        CheckoutOptions(submodules=sub_on, lfs=lfs), submodules=tuple(skipped), lfs_files=tuple(lfs_files)
    )
    return replace(handle, checkout=report, submodules=tuple(submodules))


def _sub_handle(
    path, slug, *, parent="", block=None, published_from="", branch="", note="", pinned="", root="/workspace/repo"
):
    from automation.agent.context import SubmoduleRole

    handle = make_repo_handle(
        slug, current_ref=branch or f"{path}-sha", head_detached=not branch, workspace_path=f"{root}/{path}"
    )
    return replace(
        handle,
        submodule=SubmoduleRole(
            path=path,
            parent_path=parent,
            recorded_commit=f"{path}-sha",
            target_branch="main",
            publish_block=block,
            published_from=published_from,
            pinned=pinned,
            note=note,
        ),
    )


def _skipped(path, reason, *, repo_id=None, url=""):
    from codebase.clients.checkout import SubmoduleCheckout

    return SubmoduleCheckout(path=path, parent_path="", url=url, repo_id=repo_id, skipped=reason)


class TestCheckoutSection:
    def test_an_empty_report_renders_nothing_and_leaves_the_prompt_as_it_was(self):
        from automation.agent.middlewares.git import checkout_section

        handle = make_repo_handle("g/app", workspace_path="/workspace/repo")
        old_template = GIT_SYSTEM_PROMPT.prompt.template.replace(
            "\n{{#has_checkout_sections}}\n\n{{&checkout_sections}}\n{{/has_checkout_sections}}", ""
        )
        context = {
            "git_platform": "gitlab",
            "repository": "g/app",
            "current_branch": "main",
            "default_branch": "main",
            "issue_iid": None,
            "merge_request_iid": None,
            "has_other_repositories": True,
            "other_repositories": [
                {
                    "repository": "g/mod",
                    "path": "/workspace/repos/g/mod",
                    "current_branch": "x",
                    "default_branch": "main",
                }
            ],
        }

        assert checkout_section(handle) == ""
        old = SystemMessagePromptTemplate.from_template(old_template, template_format="mustache")
        assert GIT_SYSTEM_PROMPT.format(**context).content == old.format(**context).content

    def test_the_section_lists_published_read_only_and_missing_submodules_and_lfs_files(self):
        from automation.agent.middlewares.git import SKIP_PHRASES, checkout_section
        from codebase.clients.checkout import LfsFile, PublishBlock, SkipReason, SubmoduleNote

        subs = [
            _sub_handle("libs/core", "g/core"),
            _sub_handle("libs/core/vendor/fmt", "g/fmt", parent="libs/core", branch="daiv/fmt"),
            _sub_handle("libs/moved", "g/moved", note=SubmoduleNote.BRANCH_MOVED_ON),
            _sub_handle("libs/ui", "g/ui", block=PublishBlock.PERSON_NO_WRITE),
            _sub_handle(
                "libs/shared",
                "g/shared",
                block=PublishBlock.PUBLISHED_ELSEWHERE,
                published_from="/workspace/repos/g/shared",
            ),
            _sub_handle("libs/ui/deep", "g/deep", parent="libs/ui", block=PublishBlock.PARENT_NOT_PUBLISHED),
        ]
        skipped = [
            _skipped("libs/billing", reason, repo_id="g/billing")
            for reason in SkipReason
            if reason not in (SkipReason.OUTSIDE_PLATFORM, SkipReason.OVER_LIMIT)
        ]
        skipped.append(_skipped("third_party/zlib", SkipReason.OUTSIDE_PLATFORM, url="https://github.com/madler/zlib"))
        handle = _with_checkout(
            make_repo_handle("g/app", workspace_path="/workspace/repo"),
            submodules=subs,
            skipped=skipped,
            lfs_files=[LfsFile("assets/model.bin", 10, SkipReason.OVER_LIMIT), LfsFile("ok.bin", 1)],
        )

        section = checkout_section(handle)

        assert section.startswith("## Submodules and LFS files in `g/app`\n\n- Submodules whose changes are published")
        assert "  - `libs/core` (`g/core`, into `main`).\n" in section
        assert (
            "  - `libs/core/vendor/fmt` (`g/fmt`, into `main`): continues its open "
            "merge request on its branch `daiv/fmt`." in section
        )
        assert (
            "its branch no longer contains the commit this repository records, so "
            "changes there open a new merge request" in section
        )
        assert "  - `libs/ui` (`g/ui`): the person this run belongs to can't write to that repository." in section
        assert (
            "  - `libs/shared` (`g/shared`): that repository is published from `/workspace/repos/g/shared`." in section
        )
        assert "  - `libs/ui/deep` (`g/deep`): the submodule that contains it is read-only." in section
        for reason, phrase in SKIP_PHRASES.items():
            if reason is not SkipReason.OVER_LIMIT:
                assert f": {phrase}." in section, reason
        assert "`third_party/zlib` (`https://github.com/madler/zlib`): it is outside this git platform." in section
        assert (
            "- LFS files not downloaded (they hold their pointer text): "
            "`assets/model.bin` (over the LFS download limit)." in section
        )
        assert "ok.bin" not in section
        assert section.endswith(
            "- Changes to LFS-tracked files, adding or removing a submodule, and "
            "changes inside read-only submodules are "
            "not published: the harness leaves them out of the commit and says so when your turn ends. Don't commit "
            "inside a submodule; the harness does that for the published ones."
        )

    def test_lists_stop_at_twenty(self):
        from automation.agent.middlewares.git import checkout_section
        from codebase.clients.checkout import LfsFile, SkipReason

        handle = _with_checkout(
            make_repo_handle("g/app"),
            submodules=[_sub_handle(f"libs/{i:02}", f"g/{i:02}") for i in range(23)],
            skipped=[_skipped(f"x/{i:02}", SkipReason.NOT_CATALOGUED, repo_id=f"o/{i}") for i in range(21)],
            lfs_files=[LfsFile(f"{i:02}.bin", 1, SkipReason.OVER_LIMIT) for i in range(25)],
        )

        section = checkout_section(handle)

        assert "  - and 3 more." in section and "  - and 1 more." in section
        assert "and 5 more." in section
        assert "`libs/20`" not in section and "`x/20`" not in section

    def test_the_last_bullet_follows_the_settings_that_are_on(self):
        from automation.agent.middlewares.git import checkout_section

        lfs_only = checkout_section(_with_checkout(make_repo_handle("g/app"), sub_on=False))
        sub_only = checkout_section(_with_checkout(make_repo_handle("g/app"), lfs=False))

        assert lfs_only.endswith(
            "- Changes to LFS-tracked files are not published: the harness leaves them out of "
            "the commit and says so when your turn ends."
        )
        assert sub_only.splitlines()[-1].startswith(
            "- Adding or removing a submodule, and changes inside read-only submodules"
        )

    async def test_the_prompt_carries_one_section_per_opted_in_repository_in_order(self):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        main = _with_checkout(make_repo_handle("g/app", workspace_path="/workspace/repo"))
        quiet = make_repo_handle("g/quiet")
        other = _with_checkout(make_repo_handle("g/mod"), sub_on=False)
        runtime = Mock(context=RuntimeCtx(bot_username="daiv", repos=(main, quiet, other)))
        request = MagicMock(runtime=runtime, state={}, system_prompt="")
        captured = {}

        async def handler(req):
            captured["prompt"] = req.system_prompt

        request.override = lambda **kw: MagicMock(system_prompt=kw["system_prompt"])
        await mw.awrap_model_call(request, handler)

        prompt = captured["prompt"]
        assert prompt.count("## Submodules and LFS files in") == 2
        assert prompt.index("in `g/app`") < prompt.index("in `g/mod`")
        assert "in `g/quiet`" not in prompt


class TestKeptOutNotice:
    def test_each_kind_of_change_left_out_gets_its_line(self):
        from automation.agent.middlewares.git import kept_out_notice
        from automation.agent.workspace.git_manager import KeptOut, KeptOutSubmodule
        from codebase.clients.checkout import PublishBlock

        ui = _sub_handle("libs/ui", "g/ui", block=PublishBlock.PERSON_NO_WRITE)
        core = _sub_handle("libs/core", "g/core")
        core.client.git_platform = GitPlatform.GITLAB
        root = replace(make_repo_handle("g/app"), submodules=(ui, core))
        kept_out = KeptOut(
            lfs_paths=("assets/logo.png",),
            submodules=(
                KeptOutSubmodule(
                    "libs/ui", block=PublishBlock.PERSON_NO_WRITE, changed_files=("src/button.ts",), more=2
                ),
                KeptOutSubmodule("libs/old", removed=True),
                KeptOutSubmodule("tools/vendored", added=True),
                KeptOutSubmodule("libs/core", remote_moved=True),
            ),
        )
        core_publish = RepoPublish(
            repo_id="g/core",
            merge_request=_mr(iid=12),
            published=True,
            current_ref="",
            submodule_path="libs/core",
            root_repo_id="g/app",
            kept_out=KeptOut(lfs_paths=("data/new.bin",)),
        )
        failed = RepoPublish(
            repo_id="g/fmt",
            merge_request=None,
            published=False,
            current_ref="",
            error="boom",
            submodule_path="libs/fmt",
        )

        notice = kept_out_notice(root, kept_out, [core_publish, failed], where="")

        assert notice.splitlines() == [
            "I didn't publish some of my changes:",
            "- Inside the read-only submodule `libs/ui` (the person this run belongs "
            "to can't write to that repository): "
            "`src/button.ts`, and 2 more.",
            "- The submodule `libs/old` was removed or moved, which DAIV doesn't publish.",
            "- `tools/vendored` was added as a submodule, which DAIV doesn't publish.",
            "- The submodule `libs/core` is still at the commit the branch's latest push set: someone moved it while I "
            "worked, and their commit wins. My changes there are in `g/core!12`.",
            "- The submodule `libs/fmt` couldn't be published, so its parent still points at its old commit.",
            "- LFS-tracked files: `assets/logo.png`, `libs/core/data/new.bin`.",
        ]

    def test_nothing_left_out_adds_nothing(self):
        from automation.agent.middlewares.git import kept_out_notice
        from automation.agent.workspace.git_manager import KeptOut

        assert kept_out_notice(make_repo_handle("g/app"), KeptOut(), [], where="") is None
        assert kept_out_notice(make_repo_handle("g/app"), None, [], where="") is None

    def test_a_pin_applied_or_left_gets_its_own_line(self):
        from automation.agent.middlewares.git import kept_out_notice
        from codebase.clients.checkout import SubmoduleNote

        applied = _sub_handle("libs/core", "g/core", pinned="1a2b3c4d" + "0" * 32)
        left = _sub_handle("libs/other", "g/other", note=SubmoduleNote.PIN_LEFT)
        root = replace(make_repo_handle("g/app"), submodules=(applied, left))
        pin = RepoPublish(
            repo_id="g/core",
            merge_request=None,
            published=False,
            current_ref="",
            submodule_path="libs/core",
            recorded_sha=applied.submodule.pinned,
        )

        notice = kept_out_notice(root, None, [pin], where="")

        assert notice.split("\n\n") == [
            "Updated the submodule `libs/core` to `1a2b3c4`, the commit its merge request in `g/core` was merged as.",
            "Left the submodule `libs/other` as it is: this branch no longer records "
            "the commit DAIV pushed to `g/other`.",
        ]


class TestSubmodulePublishOrder:
    def _runtime(self, *, extra=False):
        main_sub = _sub_handle("libs/lib", "g/lib")
        main = replace(make_repo_handle("g/app", workspace_path="/workspace/repo"), submodules=(main_sub,))
        repos = [main]
        if extra:
            extra_sub = _sub_handle("vendor/x", "g/x", root="/workspace/repos/g/mod")
            repos.append(replace(make_repo_handle("g/mod"), submodules=(extra_sub,)))
        return Mock(context=RuntimeCtx(bot_username="daiv", repos=tuple(repos)))

    @staticmethod
    def _sub_publish(slug, path, root, *, error=None, mr=None):
        return RepoPublish(
            repo_id=slug,
            merge_request=mr,
            published=error is None,
            current_ref="",
            pushed_sha="" if error else f"{slug}-sha",
            error=error,
            submodule_path=path,
            root_repo_id=root,
            parent_repo_id=root,
        )

    async def test_submodules_publish_before_their_repository_which_records_them(self, emit_custom_event):
        order: list[str] = []
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = self._runtime(extra=True)

        async def submodules(ctx, workspace, handle, **_):
            order.append(f"{handle.repo_id} submodules")
            path = handle.submodules[0].submodule.path
            return [self._sub_publish(handle.submodules[0].repo_id, path, handle.repo_id, mr=_mr())], {path: "rec"}

        def publish(**kwargs):
            order.append(f"publish {kwargs['scope'].record}")
            return PublishOutcome(merge_request=_mr(), published=True)

        with (
            patch("automation.agent.middlewares.git.apublish_submodules", side_effect=submodules),
            patch("automation.agent.publishers.apublish_submodules", side_effect=submodules),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
            patch("automation.agent.publishers.GitChangePublisher") as extra_cls,
            patch("automation.agent.middlewares.git.aensure_related_links", AsyncMock()) as link,
        ):
            main_cls.return_value.publish = AsyncMock(side_effect=publish)
            extra_cls.return_value.publish = AsyncMock(side_effect=publish)
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert order == [
            "g/app submodules",
            "publish {'libs/lib': 'rec'}",
            "g/mod submodules",
            "publish {'vendor/x': 'rec'}",
        ]
        publishes = [RepoPublish.from_state(p) for p in result["repo_publishes"]]
        assert [(p.repo_id, p.recorded_sha) for p in publishes] == [("g/lib", "rec"), ("g/x", "rec"), ("g/mod", "")]
        entries = link.await_args.args[0]
        assert {(e.repo_slug, e.parent_repo_id) for e in entries} >= {("g/lib", "g/app"), ("g/x", "g/mod")}

    async def test_a_failed_main_publish_after_its_submodules_is_noticed_not_raised(self, emit_custom_event):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = self._runtime()
        sub = self._sub_publish("g/lib", "libs/lib", "g/app", mr=_mr())

        with (
            patch(
                "automation.agent.middlewares.git.apublish_submodules",
                AsyncMock(return_value=([sub], {"libs/lib": "rec"})),
            ),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
            patch("automation.agent.middlewares.git.aensure_related_links", AsyncMock()),
        ):
            main_cls.return_value.publish = AsyncMock(side_effect=RuntimeError("push rejected"))
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        assert result["main_publish_error"] == "Publishing to g/app failed (RuntimeError)"
        assert [RepoPublish.from_state(p) for p in result["repo_publishes"]] == [sub]
        assert "Its submodules were published on their own" in result["messages"][0].content

    async def test_a_submodule_merge_request_links_even_without_extra_repositories(self, emit_custom_event):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = self._runtime()
        sub = self._sub_publish("g/lib", "libs/lib", "g/app", mr=_mr(iid=3))

        with (
            patch(
                "automation.agent.middlewares.git.apublish_submodules",
                AsyncMock(return_value=([sub], {"libs/lib": "rec"})),
            ),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
            patch("automation.agent.middlewares.git.aensure_related_links", AsyncMock()) as link,
        ):
            main_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            await mw.aafter_agent({"merge_request": None}, runtime)

        entries = link.await_args.args[0]
        assert [(e.repo_slug, e.parent_repo_id) for e in entries] == [("g/app", ""), ("g/lib", "g/app")]

    async def test_a_failed_submodule_publish_is_in_the_visible_notice(self, emit_custom_event):
        mw = GitMiddleware(settings=SETTINGS, workspace=FakeWorkspace())
        runtime = self._runtime()
        failed = self._sub_publish("g/lib", "libs/lib", "g/app", error="Publishing to g/lib failed (RuntimeError)")

        with (
            patch("automation.agent.middlewares.git.apublish_submodules", AsyncMock(return_value=([failed], {}))),
            patch("automation.agent.middlewares.git.GitChangePublisher") as main_cls,
        ):
            main_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            result = await mw.aafter_agent({"merge_request": None}, runtime)

        (message,) = result["messages"]
        assert "The submodule `libs/lib` couldn't be published" in message.content
        assert main_cls.return_value.publish.await_args.kwargs["scope"].record == {}
