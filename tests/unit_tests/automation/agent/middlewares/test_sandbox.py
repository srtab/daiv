import base64
import io
import tarfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

from git import Repo

from automation.agent.middlewares.file_system import DAIVFilesystemBackend
from automation.agent.middlewares.sandbox import SANDBOX_SYSTEM_PROMPT, SandboxMiddleware, _run_bash_commands
from core.conf import settings as core_settings
from core.sandbox.client import DAIVSandboxClient
from core.sandbox.schemas import RunCommandsResponse

if TYPE_CHECKING:
    from pathlib import Path


def _make_sandbox_config_mock(disallow=(), allow=()):
    """Create a mock sandbox config with command policy."""
    config = Mock()
    config.sandbox = Mock()
    config.sandbox.base_image = "python:3.12"
    config.sandbox.network_enabled = False
    config.sandbox.memory_bytes = None
    config.sandbox.cpus = None
    config.sandbox.command_policy = Mock()
    config.sandbox.command_policy.disallow = disallow
    config.sandbox.command_policy.allow = allow
    return config


def _make_agent_runtime(*, repo_working_dir: str) -> Mock:
    runtime = Mock()
    runtime.context = Mock()
    runtime.context.gitrepo = Mock(working_dir=repo_working_dir)
    runtime.context.config = _make_sandbox_config_mock()
    return runtime


def _make_bash_runtime(repo: Repo, disallow=(), allow=()) -> Mock:
    """Build a ToolRuntime-compatible mock for bash_tool tests."""
    from langchain.tools import ToolRuntime

    runtime = ToolRuntime(
        state={"session_id": "sess_1"},
        context=Mock(gitrepo=repo, config=_make_sandbox_config_mock(disallow=disallow, allow=allow)),
        config={},
        stream_writer=Mock(),
        tool_call_id="call_1",
        store=None,
    )
    return runtime


def _make_middleware(*, close_session: bool = True) -> SandboxMiddleware:
    """Build a SandboxMiddleware with dummy backend/working_dir; tests that don't exercise
    write-sync never read these values."""
    from pathlib import Path

    return SandboxMiddleware(
        backend=Mock(), agent_root="/dummy", working_dir=Path("/dummy"), close_session=close_session
    )


def _bash_tool_with_fake_client(client: Mock):
    """Build a fresh SandboxMiddleware with ``client`` pre-installed and return its bash tool."""
    middleware = _make_middleware()
    middleware._client = client
    return middleware.tools[0]


class TestBashTool:
    async def test_bash_tool_applies_patch_and_returns_results_json(self, tmp_path: Path):
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        # Configure git identity for commits
        with repo.config_writer() as writer:
            writer.set_value("user", "name", "Test User")
            writer.set_value("user", "email", "test@example.com")

        file_path = repo_dir / "hello.txt"
        file_path.write_text("old\n")
        repo.git.add("-A")
        repo.index.commit("init")

        # Create a real patch via git diff.
        file_path.write_text("new\n")
        patch_text = repo.git.diff("HEAD")
        if patch_text and not patch_text.endswith("\n"):
            patch_text += "\n"
        # Restore file so the patch application is observable.
        repo.git.checkout("--", "hello.txt")

        assert file_path.read_text() == "old\n"

        response = RunCommandsResponse(results=[], patch=base64.b64encode(patch_text.encode("utf-8")).decode("utf-8"))

        runtime = _make_bash_runtime(repo)
        client = Mock()
        client.run_commands = AsyncMock(return_value=response)
        middleware = SandboxMiddleware(
            backend=Mock(spec=DAIVFilesystemBackend),
            agent_root=f"/{repo_dir.name}",
            working_dir=repo_dir,
            close_session=True,
        )
        middleware._client = client
        bash_tool = middleware.tools[0]

        output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        assert file_path.read_text() == "new\n"
        import json as _json

        payload = _json.loads(output)
        assert payload["commands"] == []
        # The patch just edits hello.txt — nothing added/deleted/renamed.
        assert payload["files_changed"] == [{"path": "hello.txt", "op": "modified"}]
        client.run_commands.assert_awaited_once()

    async def test_bash_tool_repoless_applies_modify_patch_to_store_backend(self, tmp_path: Path):
        """End-to-end modify: store has pre-edit content; bash returns a modify patch;
        ``_apply_patch_to_backend`` seeds the staging dir from the store, runs real
        ``git apply``, and uploads the post-edit bytes back. Invariant pinned: the store
        must hold source-side bytes for ``git apply`` to match against — a regression
        that skipped the seed step would surface here as ``git apply`` failing the
        modify hunk against an empty staging dir."""
        from langchain.tools import ToolRuntime
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend
        from automation.agent.middlewares.sandbox import SandboxMiddleware

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "test-thread"))
        await backend.aupload_files([("/repo/hello.txt", b"old\n")])

        patch_text = "diff --git a/hello.txt b/hello.txt\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-old\n+new\n"
        response = RunCommandsResponse(results=[], patch=base64.b64encode(patch_text.encode("utf-8")).decode("utf-8"))

        client = Mock()
        client.run_commands = AsyncMock(return_value=response)

        middleware = SandboxMiddleware(backend=backend, agent_root="/repo", working_dir=None, close_session=True)
        middleware._client = client
        bash_tool = middleware.tools[0]

        ctx = Mock(config=_make_sandbox_config_mock())
        ctx.has_repo = False
        type(ctx).gitrepo = property(
            lambda self: (_ for _ in ()).throw(AssertionError("repoless run accessed gitrepo"))
        )
        runtime = ToolRuntime(
            state={"session_id": "sess_1"},
            context=ctx,
            config={},
            stream_writer=Mock(),
            tool_call_id="call_1",
            store=None,
        )

        output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        responses = await backend.adownload_files(["/repo/hello.txt"])
        assert responses[0].error is None
        assert responses[0].content == b"new\n"

        import json as _json

        payload = _json.loads(output)
        assert payload["files_changed"] == [{"path": "hello.txt", "op": "modified"}]
        client.run_commands.assert_awaited_once()

    async def test_bash_tool_returns_error_when_apply_patch_raises(self, tmp_path: Path):
        """If ``apply_patch_to_dir`` raises (e.g. malformed sandbox patch), the bash tool
        must surface a "Failed to persist" error string instead of returning the JSON
        success payload — otherwise the agent's local filesystem silently desyncs from
        the sandbox while bash claims success."""
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        # Non-empty patch so the response.patch branch is taken.
        response = RunCommandsResponse(
            results=[], patch=base64.b64encode(b"diff --git a/x b/x\nbogus\n").decode("utf-8")
        )

        runtime = _make_bash_runtime(repo)
        client = Mock()
        client.run_commands = AsyncMock(return_value=response)
        middleware = SandboxMiddleware(
            backend=Mock(spec=DAIVFilesystemBackend),
            agent_root=f"/{repo_dir.name}",
            working_dir=repo_dir,
            close_session=True,
        )
        middleware._client = client
        bash_tool = middleware.tools[0]

        with patch(
            "automation.agent.middlewares.sandbox.apply_patch_to_dir",
            side_effect=RuntimeError("simulated apply failure"),
        ):
            output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        assert output.startswith("error: Failed to persist")

    async def test_bash_tool_returns_error_when_sandbox_call_fails(self, tmp_path: Path):
        import httpx

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        runtime = _make_bash_runtime(repo)
        client = Mock()
        client.run_commands = AsyncMock(side_effect=httpx.RequestError("boom"))
        bash_tool = _bash_tool_with_fake_client(client)

        output = await bash_tool.coroutine(command="echo ok", runtime=runtime)

        assert output.startswith("error: Sandbox call failed")

    async def test_bash_tool_raises_when_client_not_opened(self, tmp_path: Path):
        """Calling the bash tool before ``abefore_agent`` opens the client must fail loud."""
        import pytest

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        runtime = _make_bash_runtime(repo)
        middleware = _make_middleware(close_session=True)
        bash_tool = middleware.tools[0]

        with pytest.raises(RuntimeError, match="bash tool invoked before abefore_agent"):
            await bash_tool.coroutine(command="echo ok", runtime=runtime)


class TestBashToolPolicyEnforcement:
    """
    Verify that bash_tool enforces the command policy before any sandbox call.
    All tests assert that _run_bash_commands is NOT called when a command is blocked.
    """

    async def _invoke(self, command: str, tmp_path: Path, extra_disallow=(), extra_allow=()):
        """Run bash_tool with a fresh repo and configurable policy."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True)
        repo = Repo.init(repo_dir)

        runtime = _make_bash_runtime(repo, disallow=extra_disallow, allow=extra_allow)
        runtime.tool_call_id = "call_policy"
        runtime.state["session_id"] = "sess_policy"

        client = Mock()
        run_mock = AsyncMock(return_value=RunCommandsResponse(results=[], patch=None))
        client.run_commands = run_mock
        bash_tool = _bash_tool_with_fake_client(client)

        with (
            patch.object(core_settings, "SANDBOX_COMMAND_POLICY_DISALLOW", ()),
            patch.object(core_settings, "SANDBOX_COMMAND_POLICY_ALLOW", ()),
        ):
            output = await bash_tool.coroutine(command=command, runtime=runtime)
        return output, run_mock

    # --- Default built-in disallow rules ---

    async def test_git_commit_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git commit -m 'test'", tmp_path)
        assert output.startswith("error:")
        assert "default_disallow" in output
        run_mock.assert_not_awaited()

    async def test_git_push_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git push origin main", tmp_path)
        assert output.startswith("error:")
        assert "default_disallow" in output
        run_mock.assert_not_awaited()

    async def test_git_push_force_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git push --force", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_git_reset_hard_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git reset --hard HEAD~1", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_git_config_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git config --global user.email x@y.com", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_git_rebase_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git rebase -i HEAD~3", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_git_clean_is_blocked(self, tmp_path: Path):
        output, run_mock = await self._invoke("git clean -fd", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    # --- Safe commands pass through ---

    async def test_pytest_is_allowed(self, tmp_path: Path):
        output, run_mock = await self._invoke("pytest tests/", tmp_path)
        # Policy should not block; the command reaches sandbox.
        run_mock.assert_awaited_once()

    async def test_git_status_is_allowed(self, tmp_path: Path):
        output, run_mock = await self._invoke("git status", tmp_path)
        run_mock.assert_awaited_once()

    async def test_git_diff_is_allowed(self, tmp_path: Path):
        output, run_mock = await self._invoke("git diff HEAD", tmp_path)
        run_mock.assert_awaited_once()

    async def test_git_log_is_allowed(self, tmp_path: Path):
        output, run_mock = await self._invoke("git log --oneline", tmp_path)
        run_mock.assert_awaited_once()

    async def test_make_lint_is_allowed(self, tmp_path: Path):
        output, run_mock = await self._invoke("make lint", tmp_path)
        run_mock.assert_awaited_once()

    # --- Chain bypass attempts ---

    async def test_chained_and_with_git_push_blocks_all(self, tmp_path: Path):
        """pytest && git push must block the entire invocation."""
        output, run_mock = await self._invoke("pytest tests && git push origin main", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_chained_semicolon_with_git_commit_blocks_all(self, tmp_path: Path):
        output, run_mock = await self._invoke("echo safe; git commit -m x", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_chained_pipe_with_git_reset_blocks_all(self, tmp_path: Path):
        output, run_mock = await self._invoke("cat file | git reset --hard", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    async def test_chained_or_with_git_push_blocks_all(self, tmp_path: Path):
        output, run_mock = await self._invoke("make build || git push --force", tmp_path)
        assert output.startswith("error:")
        run_mock.assert_not_awaited()

    # --- Parse failure → fail-closed ---

    async def test_unmatched_quote_blocks_execution(self, tmp_path: Path):
        output, run_mock = await self._invoke('echo "unclosed', tmp_path)
        assert output.startswith("error:")
        assert "parse" in output.lower()
        run_mock.assert_not_awaited()

    # --- Repo-level policy ---

    async def test_repo_disallow_blocks_custom_command(self, tmp_path: Path):
        output, run_mock = await self._invoke("danger cmd", tmp_path, extra_disallow=("danger cmd",))
        assert output.startswith("error:")
        assert "repo_disallow" in output
        run_mock.assert_not_awaited()

    async def test_repo_allow_does_not_override_default_disallow(self, tmp_path: Path):
        """Even if repo.allow contains 'git commit', it stays blocked."""
        output, run_mock = await self._invoke("git commit -m x", tmp_path, extra_allow=("git commit",))
        assert output.startswith("error:")
        assert "default_disallow" in output
        run_mock.assert_not_awaited()

    # --- Denial message format ---

    async def test_denial_message_contains_reason_category(self, tmp_path: Path):
        output, _ = await self._invoke("git push", tmp_path)
        assert "default_disallow" in output

    async def test_denial_message_contains_matched_rule(self, tmp_path: Path):
        output, _ = await self._invoke("git push", tmp_path)
        assert "git push" in output


class TestRunBashCommands:
    async def test_run_bash_commands_no_archive_field(self):
        """_run_bash_commands no longer tarballs the working dir; archive is gone from RunCommandsRequest."""
        run_commands_mock = AsyncMock(return_value=RunCommandsResponse(results=[], patch=None))
        client = Mock()
        client.run_commands = run_commands_mock

        response = await _run_bash_commands(client, ["echo ok"], "sess_1")

        assert response is not None
        run_commands_mock.assert_awaited_once()
        _session_id, request = run_commands_mock.call_args.args

        # The RunCommandsRequest schema no longer has an archive field.
        dumped = request.model_dump()
        assert "archive" not in dumped
        assert dumped["commands"] == ["echo ok"]


class TestApplyPatchToBackend:
    """Cover ``_apply_patch_to_backend`` branches not reachable through ``bash_tool``."""

    async def test_raises_when_filesystem_backend_has_no_working_dir(self, tmp_path: Path):
        """Misconfiguring ``SandboxMiddleware(backend=FilesystemBackend(...), working_dir=None)``
        must fail loudly, not silently no-op or write to an arbitrary directory."""
        import pytest

        from automation.agent.middlewares.file_system import DAIVFilesystemBackend
        from automation.agent.middlewares.sandbox import _apply_patch_to_backend

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        with pytest.raises(ValueError, match="working_dir"):
            await _apply_patch_to_backend(backend, "any patch", "/repo", working_dir=None)

    async def test_empty_patch_on_store_backend_is_noop(self):
        """A sandbox bash run that produced no diff (empty/whitespace patch) must not
        spin up a temp dir, call ``git apply``, or hit ``aupload_files`` — the function
        should return early."""
        from automation.agent.middlewares.file_system import DAIVStoreBackend
        from automation.agent.middlewares.sandbox import _apply_patch_to_backend

        backend = DAIVStoreBackend(namespace=("daiv-repoless-fs", "test-thread"))
        backend.aupload_files = Mock(side_effect=AssertionError("should not upload on empty patch"))  # type: ignore[method-assign]

        await _apply_patch_to_backend(backend, "", "/repo", working_dir=None)
        await _apply_patch_to_backend(backend, "   \n  ", "/repo", working_dir=None)

    async def test_delete_patch_on_store_backend_removes_entry(self):
        """A bash command that deletes a file (``rm /repo/foo.py``) produces a deletion
        patch. The walk-the-tempdir approach would silently leave the store entry behind
        because ``git apply`` removes the file from staging. Verify ``backend.delete``
        is called for the deleted path."""
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend
        from automation.agent.middlewares.sandbox import _apply_patch_to_backend

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "test-thread"))
        await backend.aupload_files([("/repo/gone.txt", b"alive\n")])

        patch_text = (
            "diff --git a/gone.txt b/gone.txt\n"
            "deleted file mode 100644\n"
            "--- a/gone.txt\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-alive\n"
        )
        await _apply_patch_to_backend(backend, patch_text, "/repo", working_dir=None)

        item = await store.aget(("daiv-repoless-fs", "test-thread"), "/repo/gone.txt")
        assert item is None, "delete patch must remove the store entry"

    async def test_rename_patch_on_store_backend_moves_entry(self):
        """A bash ``mv`` produces a rename patch. The new path must end up in the store
        with the renamed content, and the source path must be deleted — otherwise the
        store accumulates orphans every time a file is renamed."""
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend
        from automation.agent.middlewares.sandbox import _apply_patch_to_backend

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "test-thread"))
        await backend.aupload_files([("/repo/old.txt", b"hi\n")])

        patch_text = "diff --git a/old.txt b/new.txt\nsimilarity index 100%\nrename from old.txt\nrename to new.txt\n"
        await _apply_patch_to_backend(backend, patch_text, "/repo", working_dir=None)

        old = await store.aget(("daiv-repoless-fs", "test-thread"), "/repo/old.txt")
        new = await store.aget(("daiv-repoless-fs", "test-thread"), "/repo/new.txt")
        assert old is None, "rename must delete the source entry"
        assert new is not None and new.value["content"] == "hi\n"

    async def test_multi_file_patch_pairs_paths_and_contents(self):
        """One bash command can produce a patch touching multiple files in a single
        ``ApplyMutationsRequest``-equivalent. Exercises a 3-file add+modify+delete diff
        and asserts each store entry independently — a regression that mispaired
        ``upload_paths`` with ``contents`` via the ``zip(...)`` call would surface as
        swapped bytes here, not as a hard error."""
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend
        from automation.agent.middlewares.sandbox import _apply_patch_to_backend

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "multi-file"))
        await backend.aupload_files([
            ("/repo/keep.txt", b"unchanged-source\n"),
            ("/repo/edit.txt", b"line-1\n"),
            ("/repo/delete.txt", b"goodbye\n"),
        ])

        patch_text = (
            "diff --git a/edit.txt b/edit.txt\n"
            "--- a/edit.txt\n"
            "+++ b/edit.txt\n"
            "@@ -1 +1 @@\n"
            "-line-1\n"
            "+line-2\n"
            "diff --git a/delete.txt b/delete.txt\n"
            "deleted file mode 100644\n"
            "--- a/delete.txt\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-goodbye\n"
            "diff --git a/new.txt b/new.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.txt\n"
            "@@ -0,0 +1 @@\n"
            "+freshly-added\n"
        )
        await _apply_patch_to_backend(backend, patch_text, "/repo", working_dir=None)

        edited = await store.aget(("daiv-repoless-fs", "multi-file"), "/repo/edit.txt")
        deleted = await store.aget(("daiv-repoless-fs", "multi-file"), "/repo/delete.txt")
        added = await store.aget(("daiv-repoless-fs", "multi-file"), "/repo/new.txt")
        untouched = await store.aget(("daiv-repoless-fs", "multi-file"), "/repo/keep.txt")
        assert edited is not None and edited.value["content"] == "line-2\n"
        assert deleted is None
        assert added is not None and added.value["content"] == "freshly-added\n"
        # Files not referenced by the patch must not be touched.
        assert untouched is not None and untouched.value["content"] == "unchanged-source\n"

    async def test_rename_patch_with_content_change_moves_and_modifies(self):
        """A ``git mv`` followed by an edit produces a rename diff WITH hunks. The new
        path must hold the post-edit content; the source path must be deleted. This is
        the failure-prone path the pure-rename test (similarity index 100%) can't catch."""
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend
        from automation.agent.middlewares.sandbox import _apply_patch_to_backend

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "rename-modify"))
        await backend.aupload_files([("/repo/src.txt", b"hello\n")])

        patch_text = (
            "diff --git a/src.txt b/dst.txt\n"
            "similarity index 50%\n"
            "rename from src.txt\n"
            "rename to dst.txt\n"
            "--- a/src.txt\n"
            "+++ b/dst.txt\n"
            "@@ -1 +1 @@\n"
            "-hello\n"
            "+hello world\n"
        )
        await _apply_patch_to_backend(backend, patch_text, "/repo", working_dir=None)

        src = await store.aget(("daiv-repoless-fs", "rename-modify"), "/repo/src.txt")
        dst = await store.aget(("daiv-repoless-fs", "rename-modify"), "/repo/dst.txt")
        assert src is None, "rename+modify must still delete the source"
        assert dst is not None and dst.value["content"] == "hello world\n"

    async def test_add_patch_on_store_backend_creates_entry(self):
        """A bash command that creates a new file should upload the new entry to the
        store. No source-side seeding is needed (``--- /dev/null``)."""
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend
        from automation.agent.middlewares.sandbox import _apply_patch_to_backend

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "test-thread"))

        patch_text = (
            "diff --git a/new.txt b/new.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.txt\n"
            "@@ -0,0 +1 @@\n"
            "+hello\n"
        )
        await _apply_patch_to_backend(backend, patch_text, "/repo", working_dir=None)

        item = await store.aget(("daiv-repoless-fs", "test-thread"), "/repo/new.txt")
        assert item is not None
        assert item.value["content"] == "hello\n"


class TestBuildStoreArchive:
    """Direct coverage of ``_build_store_archive`` for branches not reachable through
    ``abefore_agent`` (which only sees populated/empty stores, never the error paths).

    Uses real ``GlobResult``/``FileDownloadResponse`` dataclasses instead of bare
    ``Mock(error=...)``: a regression renaming the ``error`` attribute would silently
    pass with Mocks (any attribute is truthy) but fails loudly against the dataclass.
    """

    async def test_raises_on_glob_error(self):
        """``aglob`` returning an error means the store is in a degraded state. Silently
        returning ``None`` would have ``seed_session`` skip the repo archive — turn N+1's
        bash would then see an empty workspace while the store has files. Fail loud."""
        import pytest
        from deepagents.backends.protocol import GlobResult

        from automation.agent.middlewares.sandbox import _build_store_archive

        backend = Mock()
        backend.aglob = AsyncMock(return_value=GlobResult(error="store unreachable", matches=None))
        with pytest.raises(RuntimeError, match="backend glob failed"):
            await _build_store_archive(backend, "/repo")

    async def test_raises_on_download_error(self):
        """Glob reported a path, but downloading it errored. Partial seeding is the
        wrong answer for a workspace mirror — surface the failure so ``abefore_agent``
        releases the sandbox container instead of seeding it with a half-store."""
        import pytest
        from deepagents.backends.protocol import FileDownloadResponse, GlobResult

        from automation.agent.middlewares.sandbox import _build_store_archive

        backend = Mock()
        backend.aglob = AsyncMock(
            return_value=GlobResult(error=None, matches=[{"path": "/repo/foo.py", "is_dir": False}])
        )
        backend.adownload_files = AsyncMock(
            return_value=[FileDownloadResponse(path="/repo/foo.py", content=None, error="permission_denied")]
        )
        with pytest.raises(RuntimeError, match="backend download failed"):
            await _build_store_archive(backend, "/repo")

    async def test_returns_none_for_empty_store(self):
        """Empty store on turn 1: no error, just nothing to seed. Caller's gather still
        composes a valid (skills-only) seed call."""
        from deepagents.backends.protocol import GlobResult

        from automation.agent.middlewares.sandbox import _build_store_archive

        backend = Mock()
        backend.aglob = AsyncMock(return_value=GlobResult(error=None, matches=[]))
        assert await _build_store_archive(backend, "/repo") is None

    async def test_with_composite_does_not_glob_skills_mount(self, tmp_path: Path):
        """The composite's ``aglob`` catch-all would list ``/skills/`` files too and force
        this caller to post-filter; ``_build_store_archive`` must call ``aglob`` on the
        underlying repo backend directly so the skills mount is never enumerated.
        Canary against a regression that reverts to ``backend.aglob`` on the composite.
        """
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import (
            DAIVCompositeBackend,
            DAIVFilesystemBackend,
            DAIVStoreBackend,
        )
        from automation.agent.middlewares.sandbox import _build_store_archive

        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        (skills_root / "skill-a").mkdir()
        (skills_root / "skill-a" / "SKILL.md").write_text("never-in-repo-archive\n")

        store = InMemoryStore()
        repo_backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("ns", "thread"))
        await repo_backend.aupload_files([("/repo/turn1.py", b"only this\n")])

        skills_backend = DAIVFilesystemBackend(root_dir=skills_root, virtual_mode=True)
        composite = DAIVCompositeBackend(default=repo_backend, routes={"/skills/": skills_backend})

        archive = await _build_store_archive(composite, "/repo")

        assert archive is not None
        with tarfile.open(fileobj=io.BytesIO(bytes(archive)), mode="r:gz") as tf:
            names = sorted(tf.getnames())
        assert names == ["turn1.py"], f"composite seed must not pull in skills, got {names!r}"


class TestResolveRepoBackend:
    """Validate the helper that the patch-apply and gitignore-guard dispatches rely on:
    with composite present, the underlying repo backend is unwrapped (so isinstance
    dispatch works); without composite, the backend passes through.
    """

    def test_composite_returns_underlying_default(self, tmp_path: Path):
        from automation.agent.middlewares.file_system import DAIVCompositeBackend, DAIVFilesystemBackend
        from automation.agent.middlewares.sandbox import _resolve_repo_backend

        skills = DAIVFilesystemBackend(root_dir=tmp_path / "s", virtual_mode=True)
        (tmp_path / "s").mkdir()
        repo = DAIVFilesystemBackend(root_dir=tmp_path / "r", virtual_mode=True)
        (tmp_path / "r").mkdir()
        composite = DAIVCompositeBackend(default=repo, routes={"/skills/": skills})

        assert _resolve_repo_backend(composite, "/myrepo") is repo

    def test_bare_backend_passes_through(self, tmp_path: Path):
        from automation.agent.middlewares.file_system import DAIVFilesystemBackend
        from automation.agent.middlewares.sandbox import _resolve_repo_backend

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        assert _resolve_repo_backend(backend, "/myrepo") is backend


class TestStageStorePathsToDir:
    """Direct coverage of ``_stage_store_paths_to_dir``."""

    async def test_empty_paths_list_is_noop(self, tmp_path: Path):
        """No paths to seed → don't even hit the backend."""
        from automation.agent.middlewares.sandbox import _stage_store_paths_to_dir

        backend = Mock()
        backend.adownload_files = AsyncMock(side_effect=AssertionError("must not download on empty paths"))
        await _stage_store_paths_to_dir(backend, "/repo", [], tmp_path)

    async def test_creates_nested_parent_dirs(self, tmp_path: Path):
        """A patch can reference paths inside subdirs that don't exist yet in the
        fresh staging tempdir; the helper must ``mkdir(parents=True)`` so writes
        succeed instead of failing with FileNotFoundError."""
        from automation.agent.middlewares.sandbox import _stage_store_paths_to_dir

        backend = Mock()
        from deepagents.backends.protocol import FileDownloadResponse

        backend.adownload_files = AsyncMock(
            return_value=[FileDownloadResponse(path="/repo/nested/deep/foo.py", content=b"deep", error=None)]
        )
        await _stage_store_paths_to_dir(backend, "/repo", ["nested/deep/foo.py"], tmp_path)
        assert (tmp_path / "nested" / "deep" / "foo.py").read_bytes() == b"deep"

    async def test_skips_missing_paths_so_git_apply_can_report_them(self, tmp_path: Path):
        """A genuinely missing source-side file should NOT raise here — let ``git apply``
        produce its canonical 'No such file or directory' error against the empty staging."""
        from deepagents.backends.protocol import FILE_NOT_FOUND, FileDownloadResponse

        from automation.agent.middlewares.sandbox import _stage_store_paths_to_dir

        backend = Mock()
        backend.adownload_files = AsyncMock(
            return_value=[FileDownloadResponse(path="/repo/ghost.py", content=None, error=FILE_NOT_FOUND)]
        )
        await _stage_store_paths_to_dir(backend, "/repo", ["ghost.py"], tmp_path)
        assert not (tmp_path / "ghost.py").exists()

    async def test_logs_non_file_not_found_errors_before_skipping(self, tmp_path: Path, caplog):
        """A transient/permission error must be logged before the skip — otherwise a backend
        hiccup masquerades as a missing-file path bug and operators can't diagnose it."""
        import logging

        from deepagents.backends.protocol import PERMISSION_DENIED, FileDownloadResponse

        from automation.agent.middlewares.sandbox import _stage_store_paths_to_dir

        backend = Mock()
        backend.adownload_files = AsyncMock(
            return_value=[FileDownloadResponse(path="/repo/locked.py", content=None, error=PERMISSION_DENIED)]
        )
        with caplog.at_level(logging.WARNING, logger="daiv.tools"):
            await _stage_store_paths_to_dir(backend, "/repo", ["locked.py"], tmp_path)

        assert not (tmp_path / "locked.py").exists()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("locked.py" in r.getMessage() and PERMISSION_DENIED in r.getMessage() for r in warnings), (
            f"expected a warning naming the path and error; got {[r.getMessage() for r in warnings]!r}"
        )


class TestApplyPatchToBackendErrorPropagation:
    """Production-side error surfaces of ``_apply_patch_to_backend`` — direct tests
    because integration through bash_tool catches and reformats the exception."""

    async def test_raises_when_aupload_files_returns_error(self):
        """A partial upload failure leaves the store split-brain (some files written,
        some not). Surface as ``RuntimeError`` so ``bash_tool`` can return the canonical
        ``error: Failed to persist`` string and the agent knows not to trust the run."""
        import pytest
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend
        from automation.agent.middlewares.sandbox import _apply_patch_to_backend

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "upload-err"))

        patch_text = (
            "diff --git a/x.txt b/x.txt\nnew file mode 100644\n--- /dev/null\n+++ b/x.txt\n@@ -0,0 +1 @@\n+content\n"
        )
        backend.aupload_files = AsyncMock(return_value=[Mock(error="quota exceeded")])  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="backend upload failed"):
            await _apply_patch_to_backend(backend, patch_text, "/repo", working_dir=None)

    async def test_raises_when_backend_delete_fails(self):
        """A failed delete leaves the store with files the sandbox already deleted —
        subsequent reads return stale content. Same shape as upload errors: raise so
        the agent doesn't silently see an inconsistent view."""
        import pytest
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend
        from automation.agent.middlewares.sandbox import _apply_patch_to_backend

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "delete-err"))
        await backend.aupload_files([("/repo/gone.txt", b"alive\n")])

        async def fail_delete(_path):
            return False

        backend.delete = fail_delete  # type: ignore[method-assign]

        patch_text = (
            "diff --git a/gone.txt b/gone.txt\n"
            "deleted file mode 100644\n"
            "--- a/gone.txt\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-alive\n"
        )
        with pytest.raises(RuntimeError, match="backend delete failed"):
            await _apply_patch_to_backend(backend, patch_text, "/repo", working_dir=None)


class TestBashToolStoreBackendErrorSurface:
    """End-to-end: a ``RuntimeError`` from the store-branch patch-apply must surface
    through ``bash_tool`` as the canonical ``error: Failed to persist`` string. The
    direct ``TestApplyPatchToBackendErrorPropagation`` tests stop at the boundary;
    a future refactor letting the exception escape ``bash_tool`` would not be caught
    by those — this test pins the user-facing contract for the repoless path."""

    async def test_store_backend_upload_failure_surfaces_as_persist_error(self, tmp_path: Path):
        from langchain.tools import ToolRuntime
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend
        from automation.agent.middlewares.sandbox import SandboxMiddleware

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "bash-err"))
        backend.aupload_files = AsyncMock(return_value=[Mock(error="quota exceeded")])  # type: ignore[method-assign]

        patch_text = (
            "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hi\n"
        )
        response = RunCommandsResponse(results=[], patch=base64.b64encode(patch_text.encode("utf-8")).decode("utf-8"))

        client = Mock()
        client.run_commands = AsyncMock(return_value=response)

        middleware = SandboxMiddleware(backend=backend, agent_root="/repo", working_dir=None, close_session=True)
        middleware._client = client
        bash_tool = middleware.tools[0]

        ctx = Mock(config=_make_sandbox_config_mock())
        ctx.has_repo = False
        type(ctx).gitrepo = property(
            lambda self: (_ for _ in ()).throw(AssertionError("repoless run accessed gitrepo"))
        )
        runtime = ToolRuntime(
            state={"session_id": "sess_1"},
            context=ctx,
            config={},
            stream_writer=Mock(),
            tool_call_id="call_1",
            store=None,
        )

        output = await bash_tool.coroutine(command="echo ok", runtime=runtime)
        assert output.startswith("error: Failed to persist")


class TestSandboxMiddleware:
    @staticmethod
    def _patch_client_lifecycle():
        """Stub ``DAIVSandboxClient.open``/``close`` so no real httpx.AsyncClient is created."""
        return (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=AsyncMock(return_value=None)),
        )

    async def test_abefore_agent_starts_session_and_sets_session_id(self, tmp_path: Path):
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")
        runtime = _make_agent_runtime(repo_working_dir=str(repo_dir))

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_1"),
            ) as start_session_mock,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session", new=AsyncMock(return_value=None)
            ) as seed_session_mock,
        ):
            middleware = _make_middleware(close_session=True)
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_1"}
        start_session_mock.assert_awaited_once()
        seed_session_mock.assert_awaited_once()
        _args, kwargs = seed_session_mock.call_args
        assert isinstance(kwargs.get("repo_archive"), (bytes, bytearray))
        assert kwargs.get("skills_archive") is None
        assert middleware._client is not None

    async def test_abefore_agent_closes_session_on_seed_failure(self, tmp_path: Path):
        """If seed_session raises, the started session is closed and the client is released (no leak)."""
        import pytest

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")
        runtime = _make_agent_runtime(repo_working_dir=str(repo_dir))

        close_session_mock = AsyncMock(return_value=None)
        client_close_mock = AsyncMock(return_value=None)

        with (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=client_close_mock),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_leaky"),
            ),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session",
                new=AsyncMock(side_effect=RuntimeError("simulated seed failure")),
            ),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session", new=close_session_mock),
        ):
            middleware = _make_middleware(close_session=True)
            with pytest.raises(RuntimeError, match="simulated seed failure"):
                await middleware.abefore_agent({}, runtime)

        close_session_mock.assert_awaited_once_with("sess_leaky")
        client_close_mock.assert_awaited_once()
        assert middleware._client is None

    async def test_abefore_agent_releases_client_on_start_session_failure(self, tmp_path: Path):
        """If start_session raises (sandbox unreachable / 5xx), the just-opened client is released."""
        import pytest

        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        runtime = _make_agent_runtime(repo_working_dir=str(repo_dir))

        close_session_mock = AsyncMock(return_value=None)
        client_close_mock = AsyncMock(return_value=None)

        with (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=client_close_mock),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(side_effect=RuntimeError("sandbox unreachable")),
            ),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session", new=close_session_mock),
        ):
            middleware = _make_middleware(close_session=True)
            with pytest.raises(RuntimeError, match="sandbox unreachable"):
                await middleware.abefore_agent({}, runtime)

        # No session was started, so close_session must NOT be called.
        close_session_mock.assert_not_awaited()
        # But the client we opened must be released.
        client_close_mock.assert_awaited_once()
        assert middleware._client is None

    async def test_aafter_agent_releases_client_when_close_session_raises_unexpected(self, tmp_path: Path):
        """``finally`` releases the client even when close_session raises an unhandled exception."""
        import pytest

        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_1"}

        client_close_mock = AsyncMock(return_value=None)
        with (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=client_close_mock),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session",
                new=AsyncMock(side_effect=RuntimeError("unexpected")),
            ),
        ):
            middleware = _make_middleware(close_session=True)
            middleware._client = DAIVSandboxClient()
            with pytest.raises(RuntimeError, match="unexpected"):
                await middleware.aafter_agent(state, runtime)

        client_close_mock.assert_awaited_once()
        assert middleware._client is None

    async def test_abefore_agent_reuses_session_id_when_close_session_false(self, tmp_path: Path):
        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_existing"}

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_1"),
            ) as start_session_mock,
        ):
            middleware = _make_middleware(close_session=False)
            update = await middleware.abefore_agent(state, runtime)

        assert update is None
        start_session_mock.assert_not_awaited()
        assert middleware._client is not None

    async def test_aafter_agent_closes_session_and_clears_session_id(self, tmp_path: Path):
        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_1"}

        client_close_mock = AsyncMock(return_value=None)
        with (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=client_close_mock),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session", new=AsyncMock(return_value=None)
            ) as close_session_mock,
        ):
            middleware = _make_middleware(close_session=True)
            middleware._client = DAIVSandboxClient()
            update = await middleware.aafter_agent(state, runtime)

        assert update == {"session_id": None}
        close_session_mock.assert_awaited_once_with("sess_1")
        client_close_mock.assert_awaited_once()
        assert middleware._client is None

    async def test_aafter_agent_does_not_close_session_when_close_session_false(self, tmp_path: Path):
        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))
        state = {"session_id": "sess_1"}

        client_close_mock = AsyncMock(return_value=None)
        with (
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.open", new=AsyncMock(return_value=None)),
            patch("automation.agent.middlewares.sandbox.DAIVSandboxClient.close", new=client_close_mock),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.close_session", new=AsyncMock(return_value=None)
            ) as close_session_mock,
        ):
            middleware = _make_middleware(close_session=False)
            middleware._client = DAIVSandboxClient()
            update = await middleware.aafter_agent(state, runtime)

        assert update is None
        close_session_mock.assert_not_awaited()
        # Subagent path: session is the parent's, but the client we opened still must be released.
        client_close_mock.assert_awaited_once()
        assert middleware._client is None

    async def test_abefore_agent_passes_skills_archive_when_skills_dir_populated(self, tmp_path: Path):
        repo_dir = tmp_path / "repoX"
        repo_dir.mkdir(parents=True)
        (repo_dir / "README.md").write_text("hello")

        skills_dir = tmp_path / "skills"
        (skills_dir / "skill-one").mkdir(parents=True)
        (skills_dir / "skill-one" / "SKILL.md").write_text("hi")

        runtime = _make_agent_runtime(repo_working_dir=str(repo_dir))

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch("automation.agent.middlewares.sandbox.SKILLS_CACHE_PATH", skills_dir),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_skills"),
            ),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session", new=AsyncMock(return_value=None)
            ) as seed_session_mock,
        ):
            middleware = SandboxMiddleware(
                backend=Mock(), agent_root=f"/{repo_dir.name}", working_dir=repo_dir, close_session=True
            )
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_skills"}
        seed_session_mock.assert_awaited_once()
        _args, kwargs = seed_session_mock.call_args
        assert isinstance(kwargs.get("repo_archive"), (bytes, bytearray))
        assert isinstance(kwargs.get("skills_archive"), (bytes, bytearray))

    async def test_abefore_agent_repoless_with_empty_store_seeds_only_skills_archive(self, tmp_path: Path):
        """Turn 1 of a repoless thread: the store has no prior writes, so the repo archive
        is ``None`` and only the skills archive is seeded into the fresh sandbox container."""
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend

        agent_path = tmp_path / "repo"
        agent_path.mkdir(parents=True)

        skills_dir = tmp_path / "skills"
        (skills_dir / "skill-one").mkdir(parents=True)
        (skills_dir / "skill-one" / "SKILL.md").write_text("hi")

        runtime = Mock()
        runtime.context = Mock()
        runtime.context.has_repo = False
        type(runtime.context).gitrepo = property(
            lambda self: (_ for _ in ()).throw(AssertionError("repoless run accessed gitrepo"))
        )
        runtime.context.config = _make_sandbox_config_mock()

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "empty-thread"))

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch("automation.agent.middlewares.sandbox.SKILLS_CACHE_PATH", skills_dir),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_repoless"),
            ),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session", new=AsyncMock(return_value=None)
            ) as seed_session_mock,
        ):
            middleware = SandboxMiddleware(
                backend=backend, agent_root=f"/{agent_path.name}", working_dir=agent_path, close_session=True
            )
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_repoless"}
        seed_session_mock.assert_awaited_once()
        _args, kwargs = seed_session_mock.call_args
        assert kwargs.get("repo_archive") is None
        assert isinstance(kwargs.get("skills_archive"), (bytes, bytearray))

    async def test_abefore_agent_repoless_seeds_store_contents_as_repo_archive(self, tmp_path: Path):
        """Multi-turn: the store has files from prior turns. ``abefore_agent`` builds a
        repo archive from the current store state so the freshly-started sandbox container
        sees those files — without this, ``bash`` on turn 2 can't read what ``write_file``
        wrote on turn 1."""
        import io
        import tarfile

        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend

        runtime = Mock()
        runtime.context = Mock()
        runtime.context.has_repo = False
        type(runtime.context).gitrepo = property(
            lambda self: (_ for _ in ()).throw(AssertionError("repoless run accessed gitrepo"))
        )
        runtime.context.config = _make_sandbox_config_mock()

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "multi-turn"))
        await backend.aupload_files([
            ("/repo/turn1.py", b"print('from turn 1')\n"),
            ("/repo/nested/note.md", b"persisted across turns\n"),
        ])

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_turn2"),
            ),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session", new=AsyncMock(return_value=None)
            ) as seed_session_mock,
        ):
            middleware = SandboxMiddleware(backend=backend, agent_root="/repo", working_dir=None, close_session=True)
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_turn2"}
        seed_session_mock.assert_awaited_once()
        _args, kwargs = seed_session_mock.call_args
        repo_archive = kwargs.get("repo_archive")
        assert isinstance(repo_archive, (bytes, bytearray)), "store-backed multi-turn must seed a repo archive"

        # Verify bytes (not just names): a zip/ordering regression in _build_store_archive
        # would silently swap contents but keep names matching.
        with tarfile.open(fileobj=io.BytesIO(bytes(repo_archive)), mode="r:gz") as tf:
            members = {m.name: tf.extractfile(m).read() for m in tf.getmembers() if tf.extractfile(m) is not None}
        assert members == {"turn1.py": b"print('from turn 1')\n", "nested/note.md": b"persisted across turns\n"}

    async def test_abefore_agent_repoless_skips_seed_when_no_skills_or_store(self, tmp_path: Path):
        """Empty store + no skills available → no archives → ``seed_session`` not called.
        The sandbox API rejects empty seeds, so we must short-circuit before hitting it."""
        from langgraph.store.memory import InMemoryStore

        from automation.agent.middlewares.file_system import DAIVStoreBackend

        runtime = Mock()
        runtime.context = Mock()
        runtime.context.has_repo = False
        type(runtime.context).gitrepo = property(
            lambda self: (_ for _ in ()).throw(AssertionError("repoless run accessed gitrepo"))
        )
        runtime.context.config = _make_sandbox_config_mock()

        store = InMemoryStore()
        backend = DAIVStoreBackend(store=store, namespace=lambda _rt: ("daiv-repoless-fs", "no-content"))

        empty_skills = tmp_path / "skills-empty"
        empty_skills.mkdir()

        open_patch, close_patch = self._patch_client_lifecycle()
        with (
            open_patch,
            close_patch,
            patch("automation.agent.middlewares.sandbox.SKILLS_CACHE_PATH", empty_skills),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.start_session",
                new=AsyncMock(return_value="sess_no_seed"),
            ),
            patch(
                "automation.agent.middlewares.sandbox.DAIVSandboxClient.seed_session", new=AsyncMock(return_value=None)
            ) as seed_session_mock,
        ):
            middleware = SandboxMiddleware(backend=backend, agent_root="/repo", working_dir=None, close_session=True)
            update = await middleware.abefore_agent({}, runtime)

        assert update == {"session_id": "sess_no_seed"}
        seed_session_mock.assert_not_awaited()

    def test_make_skills_archive_returns_none_when_dir_missing(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_skills_archive

        assert _make_skills_archive(tmp_path / "nonexistent") is None

    def test_make_skills_archive_returns_none_when_dir_empty(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_skills_archive

        empty = tmp_path / "skills"
        empty.mkdir()
        assert _make_skills_archive(empty) is None

    def test_make_skills_archive_packs_children_relative_to_root(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_skills_archive

        skills = tmp_path / "skills"
        (skills / "skill-one").mkdir(parents=True)
        (skills / "skill-one" / "SKILL.md").write_text("hello")
        (skills / "skill-two").mkdir()
        (skills / "skill-two" / "SKILL.md").write_text("world")

        archive = _make_skills_archive(skills)
        assert isinstance(archive, bytes)

        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
            names = sorted(tf.getnames())
        assert "skill-one/SKILL.md" in names
        assert "skill-two/SKILL.md" in names
        # No top-level wrapper directory.
        assert not any(n.startswith("skills/") for n in names)

    def test_make_skills_archive_returns_none_on_iterdir_oserror(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_skills_archive

        skills = tmp_path / "skills"
        skills.mkdir()

        with patch("automation.agent.middlewares.sandbox.Path.iterdir", side_effect=PermissionError("denied")):
            result = _make_skills_archive(skills)

        assert result is None

    def test_make_skills_archive_returns_none_on_tar_error(self, tmp_path: Path):
        from automation.agent.middlewares.sandbox import _make_skills_archive

        skills = tmp_path / "skills"
        (skills / "skill-one").mkdir(parents=True)
        (skills / "skill-one" / "SKILL.md").write_text("hello")

        with patch("automation.agent.middlewares.sandbox.tarfile.open", side_effect=tarfile.TarError("boom")):
            result = _make_skills_archive(skills)

        assert result is None

    async def test_awrap_model_call_appends_sandbox_system_prompt(self, tmp_path: Path):
        from langchain.agents.middleware import ModelRequest, ModelResponse

        runtime = _make_agent_runtime(repo_working_dir=str(tmp_path / "repoX"))

        middleware = _make_middleware()

        seen_prompt: str | None = None

        async def handler(request: ModelRequest) -> ModelResponse:
            nonlocal seen_prompt
            seen_prompt = request.system_prompt
            return ModelResponse(result=[])

        request = ModelRequest(model=Mock(), messages=[], system_prompt="base prompt", state=Mock(), runtime=runtime)

        _ = await middleware.awrap_model_call(request, handler)
        assert seen_prompt is not None
        assert seen_prompt.startswith("base prompt")
        assert SANDBOX_SYSTEM_PROMPT in seen_prompt


class TestAwrapToolCall:
    """Tool dispatch is intercepted at ToolNode level, not via ``awrap_model_call``.

    These tests pin the entry point: ``awrap_tool_call`` must intercept dispatched
    write_file/edit_file calls so that the mirror runs against ``ToolNode.tools_by_name``,
    which is built once at agent-creation time and not updated by ``awrap_model_call``.
    """

    @staticmethod
    def _request(tool_name: str, args: dict, *, runtime=None):
        from langgraph.prebuilt.tool_node import ToolCallRequest

        tool = Mock(spec_set=["name"])
        tool.name = tool_name
        return ToolCallRequest(
            tool_call={"name": tool_name, "args": args, "id": "call_1", "type": "tool_call"},
            tool=tool,
            state={},
            runtime=runtime or Mock(),
        )

    async def test_passes_through_when_tool_is_not_write_or_edit(self, tmp_path: Path):
        request = self._request("read_file", {"file_path": "/repo/foo.py"})
        middleware = _make_middleware()
        middleware._syncer = Mock()  # set so we don't bypass on syncer-missing path

        sentinel = object()

        async def handler(req):
            assert req is request
            return sentinel

        result = await middleware.awrap_tool_call(request, handler)
        assert result is sentinel

    async def test_passes_through_when_syncer_not_initialized(self, tmp_path: Path):
        """Pre-``abefore_agent`` dispatch is rare but must not raise."""
        request = self._request("write_file", {"file_path": "/repo/foo.py", "content": "x"})
        middleware = _make_middleware()
        # _syncer is None — abefore_agent never ran.

        sentinel = object()

        async def handler(req):
            return sentinel

        result = await middleware.awrap_tool_call(request, handler)
        assert result is sentinel

    async def test_passes_through_when_tool_is_none(self, tmp_path: Path):
        """Unregistered tool calls reach awrap_tool_call with ``request.tool=None``."""
        from langgraph.prebuilt.tool_node import ToolCallRequest

        request = ToolCallRequest(
            tool_call={"name": "unknown", "args": {}, "id": "call_1", "type": "tool_call"},
            tool=None,
            state={},
            runtime=Mock(),
        )
        middleware = _make_middleware()
        middleware._syncer = Mock()

        sentinel = object()

        async def handler(req):
            return sentinel

        result = await middleware.awrap_tool_call(request, handler)
        assert result is sentinel

    async def test_write_file_refused_outside_agent_root(self, tmp_path: Path):
        """A write to ``/skills/...`` (or any path outside agent_root) must be refused
        before dispatch. The skills mount is shared across concurrent agent runs in this
        process; an errant write here would later trigger a rollback ``backend.delete``
        that clobbers a file other runs depend on.
        """
        from langchain_core.messages import ToolMessage

        from automation.agent.middlewares.file_system import SandboxSyncer

        backend = Mock(spec=DAIVFilesystemBackend)
        runtime = Mock()
        runtime.context = Mock(has_repo=True)
        runtime.state = {"session_id": "sess_1"}

        middleware = _make_middleware()
        middleware._backend = backend
        middleware._agent_root = "/myrepo"
        middleware._syncer = SandboxSyncer(backend=backend, agent_root="/myrepo", client=Mock())

        request = self._request(
            "write_file", {"file_path": "/skills/some-skill/SKILL.md", "content": "x"}, runtime=runtime
        )
        handler = AsyncMock()

        result = await middleware.awrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "Refused" in result.content
        assert "/myrepo/" in result.content
        handler.assert_not_called()
        backend.delete.assert_not_called()

    async def test_write_file_dispatches_when_resolve_target_fails(self, tmp_path: Path):
        """Pre-dispatch path-resolution failure must fall through to upstream, not
        synthesize a refusal — upstream owns the canonical "invalid path" error."""
        from langchain_core.messages import ToolMessage

        from automation.agent.middlewares.file_system import SandboxSyncer

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo = Repo.init(repo_dir)

        backend = Mock(spec=DAIVFilesystemBackend)
        backend._resolve_path = Mock(side_effect=ValueError("traversal"))

        runtime = Mock()
        runtime.context = Mock(gitrepo=repo, has_repo=True)
        runtime.state = {"session_id": "sess_1"}

        middleware = _make_middleware()
        middleware._backend = backend
        middleware._agent_root = f"/{repo_dir.name}"
        middleware._syncer = SandboxSyncer(backend=backend, agent_root=f"/{repo_dir.name}", client=Mock())

        request = self._request("write_file", {"file_path": f"/{repo_dir.name}/foo", "content": "x"}, runtime=runtime)
        sentinel = ToolMessage(content="upstream error", tool_call_id="call_1", name="write_file", status="error")
        handler = AsyncMock(return_value=sentinel)

        result = await middleware.awrap_tool_call(request, handler)

        handler.assert_called_once()
        assert result is sentinel

    async def test_write_file_refused_when_path_is_gitignored(self, tmp_path: Path):
        """write_file on a `.gitignore`-matching path must be refused before the upstream
        handler runs — `git add -A` would silently drop the file, so the agent would
        report success while the change never reached the merge request."""
        from langchain_core.messages import ToolMessage

        from automation.agent.middlewares.file_system import SandboxSyncer

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo = Repo.init(repo_dir)
        (repo_dir / ".gitignore").write_text(".python-version\n")

        target = repo_dir / ".python-version"

        backend = Mock(spec=DAIVFilesystemBackend)
        backend._resolve_path = Mock(return_value=str(target))

        runtime = Mock()
        runtime.context = Mock(gitrepo=repo, has_repo=True)
        runtime.state = {"session_id": "sess_1"}

        middleware = _make_middleware()
        middleware._backend = backend
        middleware._agent_root = f"/{repo_dir.name}"
        middleware._syncer = SandboxSyncer(backend=backend, agent_root=f"/{repo_dir.name}", client=Mock())

        request = self._request(
            "write_file", {"file_path": f"/{repo_dir.name}/.python-version", "content": "3.11\n"}, runtime=runtime
        )
        handler = AsyncMock()

        result = await middleware.awrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert ".gitignore" in result.content
        assert not target.exists()
        handler.assert_not_called()

    async def test_write_file_refused_when_gitignore_check_unknown(self, tmp_path: Path):
        """If `git check-ignore` itself fails (corrupt repo, missing binary, permissions),
        the helper returns ``IgnoreCheck.UNKNOWN`` and the write must be refused — failing
        open here would silently re-introduce the `git add -A` drop the guard prevents."""
        from langchain_core.messages import ToolMessage

        from automation.agent.middlewares.file_system import SandboxSyncer
        from codebase.utils import GitManager, IgnoreCheck

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo = Repo.init(repo_dir)
        target = repo_dir / "foo.py"

        backend = Mock(spec=DAIVFilesystemBackend)
        backend._resolve_path = Mock(return_value=str(target))

        runtime = Mock()
        runtime.context = Mock(gitrepo=repo, has_repo=True)
        runtime.state = {"session_id": "sess_1"}

        middleware = _make_middleware()
        middleware._backend = backend
        middleware._agent_root = f"/{repo_dir.name}"
        middleware._syncer = SandboxSyncer(backend=backend, agent_root=f"/{repo_dir.name}", client=Mock())

        request = self._request(
            "write_file", {"file_path": f"/{repo_dir.name}/foo.py", "content": "x\n"}, runtime=runtime
        )
        handler = AsyncMock()

        with patch.object(GitManager, "is_path_ignored", return_value=IgnoreCheck.UNKNOWN):
            result = await middleware.awrap_tool_call(request, handler)

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "could not determine" in result.content
        handler.assert_not_called()

    async def test_write_file_repoless_skips_gitignore_check(self, tmp_path: Path):
        """Repoless runs (``runtime.context.has_repo is False``) have no MR target and no
        gitrepo; the gitignore precheck must be skipped, not raise ``SingleRepoRequiredError``
        or block the write."""
        from langchain_core.messages import ToolMessage

        from automation.agent.middlewares.file_system import SandboxSyncer
        from codebase.utils import GitManager

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        target = repo_dir / "foo.py"
        target.write_text("x")

        backend = Mock(spec=DAIVFilesystemBackend)
        backend._resolve_path = Mock(return_value=str(target))

        runtime = Mock()
        # has_repo=False; gitrepo deliberately raises so any access bypassing the gate fails loudly.
        type(runtime.context).gitrepo = property(
            lambda self: (_ for _ in ()).throw(AssertionError("repoless run accessed gitrepo"))
        )
        runtime.context.has_repo = False
        runtime.state = {"session_id": "sess_1"}

        middleware = _make_middleware()
        middleware._agent_root = f"/{repo_dir.name}"
        syncer = SandboxSyncer(backend=backend, agent_root=f"/{repo_dir.name}", client=Mock())
        syncer.lock = AsyncMock()
        syncer.lock.__aenter__ = AsyncMock()
        syncer.lock.__aexit__ = AsyncMock()
        syncer.mirror = AsyncMock(return_value=None)
        middleware._syncer = syncer

        request = self._request(
            "write_file", {"file_path": f"/{repo_dir.name}/foo.py", "content": "x"}, runtime=runtime
        )
        ok_message = ToolMessage(content="Successfully wrote", tool_call_id="call_1", name="write_file")
        handler = AsyncMock(return_value=ok_message)

        # GitManager must not be constructed in repoless mode.
        with patch.object(GitManager, "__init__", side_effect=AssertionError("GitManager built in repoless run")):
            result = await middleware.awrap_tool_call(request, handler)

        handler.assert_called_once()
        assert isinstance(result, ToolMessage)
        assert result.status != "error"
