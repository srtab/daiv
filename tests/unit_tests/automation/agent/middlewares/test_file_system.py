from __future__ import annotations

import stat
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware as UpstreamFilesystemMiddleware
from deepagents.middleware.filesystem import _check_fs_permission
from langchain_core.messages import ToolMessage

from automation.agent.middlewares import file_system as fs_module
from automation.agent.middlewares.file_system import (
    EDIT_SUCCESS_PREFIX,
    WORKSPACE_FENCE_PERMISSIONS,
    WRITE_SUCCESS_PREFIX,
    DAIVFilesystemBackend,
    build_disk_workspace_backend,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def working_repo(tmp_path) -> Path:
    """Test layout: tmp_path/myrepo/."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    return repo


@pytest.fixture
def setup(working_repo):
    """Disk-backed repo backend + the upstream filesystem tool map."""
    backend = DAIVFilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    fs = UpstreamFilesystemMiddleware(backend=backend, custom_tool_descriptions=fs_module.CUSTOM_TOOL_DESCRIPTIONS)
    tools = {tool.name: tool for tool in fs.tools}
    return SimpleNamespace(backend=backend, tools=tools, repo=working_repo)


def _runtime(*, state: dict[str, Any], working_dir: Path) -> SimpleNamespace:
    """A minimal ``ToolRuntime`` shim sufficient for the upstream filesystem tools."""
    return SimpleNamespace(
        state=state,
        context=SimpleNamespace(gitrepo=SimpleNamespace(working_dir=str(working_dir)), has_repo=True),
        tool_call_id="call_test",
    )


# ---------------------------------------------------------------------------
# Upstream contract pinning
# ---------------------------------------------------------------------------


async def test_upstream_success_prefixes_remain_stable(setup):
    """Pin the upstream write/edit success-message prefixes.

    ``WRITE_SUCCESS_PREFIX``/``EDIT_SUCCESS_PREFIX`` mirror deepagents' filesystem-tool
    success strings; pinning them makes a deepagents reword fail this test instead of
    silently drifting from the constants DAIV keeps in lockstep with upstream.
    """
    runtime = _runtime(state={}, working_dir=setup.repo)

    write_result = await setup.tools["write_file"].coroutine(
        file_path=f"/{setup.repo.name}/contract.py", content="x", runtime=runtime
    )
    write_text = write_result.content if isinstance(write_result, ToolMessage) else write_result
    assert write_text.startswith(WRITE_SUCCESS_PREFIX), (
        f"upstream changed write success format; update WRITE_SUCCESS_PREFIX: {write_text!r}"
    )

    edit_result = await setup.tools["edit_file"].coroutine(
        file_path=f"/{setup.repo.name}/contract.py", old_string="x", new_string="y", runtime=runtime
    )
    edit_text = edit_result.content if isinstance(edit_result, ToolMessage) else edit_result
    assert edit_text.startswith(EDIT_SUCCESS_PREFIX), (
        f"upstream changed edit success format; update EDIT_SUCCESS_PREFIX: {edit_text!r}"
    )


class TestDAIVCompositeBackend:
    """Composite routing must preserve the prefix-stripping invariant for DAIV's two
    extension methods (``delete``/``stat_mode``) and the dispatch helper
    (``resolve_backend_for``), so a routed op reaches the right underlying backend with
    the route prefix stripped.
    """

    @staticmethod
    def _make_composite(tmp_path: Path):
        """Compose a repo + skills backend pair the same way the disk-backed path does.

        ``DAIVFilesystemBackend(virtual_mode=True)`` interprets virtual paths as
        ``/<root_dir.name>/<rel>`` and resolves them to ``root_dir.parent/<root_dir.name>/<rel>``,
        so each backend's ``root_dir`` must literally exist on disk. We layer ``repo-mount``
        and ``skills-mount`` under ``tmp_path`` and address them via ``/repo-mount/...`` and
        ``/skills/...`` respectively.

        The skills route uses an unrelated virtual prefix on purpose: the route strips
        ``/skills/`` before delegating to a backend whose virtual root is ``/skills-mount``,
        and ``_resolve_path("foo.md")`` happens to land under ``skills-mount`` because
        ``virtual_mode`` falls back to root-relative for paths without the agent prefix.
        """
        from automation.agent.middlewares.file_system import DAIVCompositeBackend

        skills_root = tmp_path / "skills-mount"
        skills_root.mkdir()
        repo_root = tmp_path / "repo-mount"
        repo_root.mkdir()
        skills = DAIVFilesystemBackend(root_dir=skills_root, virtual_mode=True)
        repo = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        composite = DAIVCompositeBackend(default=repo, routes={"/skills/": skills})
        return composite, skills, repo, skills_root, repo_root

    async def test_delete_strips_prefix_for_routed_path(self, tmp_path: Path):
        composite, _skills, _repo, skills_root, _repo_root = self._make_composite(tmp_path)
        (skills_root / "foo.md").write_text("x")

        ok = await composite.delete("/skills/foo.md")

        assert ok is True
        assert not (skills_root / "foo.md").exists()

    async def test_delete_passes_default_path_unchanged(self, tmp_path: Path):
        composite, _skills, _repo, _skills_root, repo_root = self._make_composite(tmp_path)
        target = repo_root / "bar.py"
        target.write_text("x")

        ok = await composite.delete("/repo-mount/bar.py")

        assert ok is True
        assert not target.exists(), "default-route delete must operate on the unstripped path"

    async def test_stat_mode_routes_to_underlying_backend(self, tmp_path: Path):
        composite, _skills, _repo, skills_root, repo_root = self._make_composite(tmp_path)
        skills_target = skills_root / "exec.sh"
        skills_target.write_text("#!/bin/sh\n")
        skills_target.chmod(0o755)
        repo_target = repo_root / "plain.py"
        repo_target.write_text("x")
        repo_target.chmod(0o644)

        skills_mode = await composite.stat_mode("/skills/exec.sh")
        repo_mode = await composite.stat_mode("/repo-mount/plain.py")

        assert stat.S_IMODE(skills_mode) == 0o755, "skills-routed stat_mode reads real bits"
        assert stat.S_IMODE(repo_mode) == 0o644, "default-routed stat_mode reads real bits"

    async def test_agrep_subbackend_error_passes_through(self, tmp_path: Path):
        """A genuine backend failure (grep never ran) must pass through verbatim so the model
        sees the real error rather than a misleading "no matches"."""
        from deepagents.backends.protocol import GrepResult

        composite, _skills, _repo, _skills_root, _repo_root = self._make_composite(tmp_path)

        async def failing_agrep(*args, **kwargs):
            return GrepResult(error="grep failed")

        composite.default.agrep = failing_agrep

        result = await composite.agrep("foo|bar")

        assert result.error == "grep failed"

    async def test_resolve_backend_for_returns_route_target(self, tmp_path: Path):
        composite, skills, repo, _skills_root, _repo_root = self._make_composite(tmp_path)

        assert composite.resolve_backend_for("/skills/foo") is skills
        assert composite.resolve_backend_for("/skills/") is skills
        assert composite.resolve_backend_for("/repo-mount/bar") is repo
        assert composite.resolve_backend_for("/random/baz") is repo, "unrouted falls through to default"

    async def test_constructor_rejects_backend_missing_daiv_methods(self, tmp_path: Path):
        from automation.agent.middlewares.file_system import DAIVCompositeBackend

        good = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        plain_state = SimpleNamespace()  # missing delete + stat_mode

        with pytest.raises(TypeError, match="DAIVBackendProtocol"):
            DAIVCompositeBackend(default=good, routes={"/x/": plain_state})  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="DAIVBackendProtocol"):
            DAIVCompositeBackend(default=plain_state, routes={})  # type: ignore[arg-type]


def test_bind_session_sets_session_with_construction_client():
    from automation.agent.middlewares.file_system import SandboxFileBackend

    backend = SandboxFileBackend(client=object())
    backend.bind_session("sess-1")
    assert backend._session_id == "sess-1"


def test_bind_session_rejects_cross_session_rebind():
    from automation.agent.middlewares.file_system import SandboxFileBackend

    backend = SandboxFileBackend(client=object(), session_id="sess-1")
    with pytest.raises(RuntimeError, match="refusing"):
        backend.bind_session("sess-2")


def test_filesystem_absolute_path_directive_names_the_repo_root():
    """The directive names where repo files live so the model uses the full path instead of a
    repo-relative slip (e.g. /daiv/...), without claiming that root is the only valid location."""
    from automation.agent.middlewares.file_system import filesystem_absolute_path_directive

    directive = filesystem_absolute_path_directive("/workspace/repo/")
    assert "/workspace/repo/" in directive
    assert "/workspace/repo/path/to/file.py" in directive  # full-path example
    # Must NOT forbid the scratchpad / skills the sandbox prompt tells the model it may use.
    assert "rejected" not in directive
    assert "only" not in directive.lower()


def test_filesystem_absolute_path_directive_normalizes_trailing_slash():
    """Defensive: a caller passing the root without a trailing slash still yields a single one."""
    from automation.agent.middlewares.file_system import filesystem_absolute_path_directive

    assert "/workspace/repo/path/to/file.py" in filesystem_absolute_path_directive("/workspace/repo")


class TestWorkspaceFencePermissions:
    """Disk-mode fence: allow read+write under the three real subtrees, read-only access to the
    offloaded-artifact dirs (so eviction read-back works), and deny the bare /workspace root plus
    any other path beneath it."""

    def test_allows_real_subtrees(self):
        for op in ("read", "write"):
            for path in (
                "/workspace/repo",
                "/workspace/repo/daiv/foo.py",
                "/workspace/skills",
                "/workspace/skills/code-review/SKILL.md",
                "/workspace/tmp",
                "/workspace/tmp/scratch.txt",
            ):
                assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, op, path) == "allow", (op, path)

    def test_denies_bare_root_and_unrelated_workspace_paths(self):
        # Bare /workspace (the deny needs the literal pattern; /workspace/** does not match it) and
        # any path under /workspace that isn't a real subtree or an artifact dir are denied both ways.
        for op in ("read", "write"):
            for path in ("/workspace", "/workspace/random", "/workspace/repofoo", "/workspace/repofoo/x"):
                assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, op, path) == "deny", (op, path)

    def test_artifact_dirs_are_readable_but_not_writable(self):
        # Eviction / output_to_file write here through the backend directly (bypassing the fence);
        # the agent must be able to read the offloaded file back, but never writes here itself.
        for path in (
            "/workspace/large_tool_results",
            "/workspace/large_tool_results/call_abc",
            "/workspace/conversation_history",
            "/workspace/conversation_history/uuid.md",
        ):
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "read", path) == "allow", path
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "write", path) == "deny", path

    def test_fence_allows_framework_offload_prefixes(self, tmp_path):
        """Drift-guard: the artifact read carve-out must cover whatever offload prefixes deepagents
        derives from ``artifacts_root``. If a framework bump renames those dirs, this fails loudly
        instead of silently re-breaking offload read-back in disk mode."""
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        backend = build_disk_workspace_backend(clone_dir, skills_cache=tmp_path / "skills_cache")
        middleware = UpstreamFilesystemMiddleware(backend=backend, _permissions=WORKSPACE_FENCE_PERMISSIONS)

        for prefix in (middleware._large_tool_results_prefix, middleware._conversation_history_prefix):
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "read", f"{prefix}/some-id") == "allow", prefix
            assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "write", f"{prefix}/some-id") == "deny", prefix

    def test_paths_outside_workspace_default_allow(self):
        assert _check_fs_permission(WORKSPACE_FENCE_PERMISSIONS, "read", "/etc/passwd") == "allow"


class TestBuildDiskWorkspaceBackend:
    """The disk composite maps the unified /workspace namespace onto local disk locations."""

    async def test_routes_repo_skills_and_tmp(self, tmp_path):
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        skills_cache = tmp_path / "skills_cache"
        skills_cache.mkdir()

        backend = build_disk_workspace_backend(clone_dir, skills_cache=skills_cache)

        assert (await backend.awrite("/workspace/repo/a.txt", "R")).error is None
        assert (clone_dir / "a.txt").read_text() == "R"

        assert (await backend.awrite("/workspace/skills/s.txt", "S")).error is None
        assert (skills_cache / "s.txt").read_text() == "S"

        assert (await backend.awrite("/workspace/tmp/t.txt", "T")).error is None
        assert (clone_dir.parent / "workspace" / "tmp" / "t.txt").read_text() == "T"

    def test_artifacts_root_is_workspace(self, tmp_path):
        clone_dir = tmp_path / "repo"
        clone_dir.mkdir()
        backend = build_disk_workspace_backend(clone_dir, skills_cache=tmp_path / "skills_cache")
        assert backend.artifacts_root == "/workspace"


# ---------------------------------------------------------------------------
# DAIVFilesystemMiddleware (extended grep)
# ---------------------------------------------------------------------------


class TestDAIVFilesystemMiddleware:
    """DAIV's FilesystemMiddleware subclass that exposes the extended (ripgrep) grep signature."""

    def _middlewares(self, root):
        from automation.agent.middlewares.file_system import DAIVFilesystemMiddleware

        backend = DAIVFilesystemBackend(root_dir=root, virtual_mode=True)
        daiv = DAIVFilesystemMiddleware(backend=backend)
        upstream = UpstreamFilesystemMiddleware(backend=backend)
        return daiv, upstream

    def test_tool_name_set_matches_upstream(self, tmp_path):
        """A deepagents bump that adds/removes a filesystem tool must fail loudly here rather than
        silently changing the subclass's surface (it only overrides grep, inheriting the rest)."""
        daiv, upstream = self._middlewares(tmp_path)
        assert {t.name for t in daiv.tools} == {t.name for t in upstream.tools}

    def test_grep_tool_schema_exposes_extended_fields(self, tmp_path):
        """The grep tool keeps upstream's params and adds DAIV's ripgrep options."""
        daiv, _ = self._middlewares(tmp_path)
        grep = next(t for t in daiv.tools if t.name == "grep")
        fields = set(grep.args.keys())
        assert {"pattern", "path", "glob", "output_mode"} <= fields, "upstream params must remain"
        assert {"head_limit", "case_insensitive", "multiline"} <= fields, "extended params must be present"

    def test_subclass_name_differs_from_upstream(self, tmp_path):
        """The subclass must NOT share upstream's ``.name`` — langchain rejects two middleware with
        the same name in one stack, and the main agent appends this beside framework middleware."""
        daiv, upstream = self._middlewares(tmp_path)
        assert daiv.name == "DAIVFilesystemMiddleware"
        assert daiv.name != upstream.name

    @staticmethod
    def _grep_coroutine(middleware):
        return next(t for t in middleware.tools if t.name == "grep").coroutine

    async def test_grep_tool_denies_read_outside_fence(self, tmp_path):
        """The read-permission gate must reject a fenced path with an error ToolMessage BEFORE any
        backend call — a regression here would let a fenced subagent grep outside its subtree."""
        from automation.agent.middlewares.file_system import DAIVFilesystemMiddleware

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        daiv = DAIVFilesystemMiddleware(backend=backend, _permissions=WORKSPACE_FENCE_PERMISSIONS)
        grep = self._grep_coroutine(daiv)

        # `/workspace/secret` is denied by the fence (only repo/skills/tmp subtrees are allowed).
        msg = await grep(pattern="x", runtime=_runtime(state={}, working_dir=tmp_path), path="/workspace/secret")

        assert msg.status == "error"
        assert "permission denied" in msg.content.lower()

    async def test_grep_tool_rejects_traversal_path(self, tmp_path):
        """Path validation must reject a traversal path with an error ToolMessage, before any
        backend call (a relative path is normalized by validate_path, but ``..`` is rejected)."""
        from automation.agent.middlewares.file_system import DAIVFilesystemMiddleware

        backend = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        daiv = DAIVFilesystemMiddleware(backend=backend)
        grep = self._grep_coroutine(daiv)

        msg = await grep(pattern="x", runtime=_runtime(state={}, working_dir=tmp_path), path="../escape")

        assert msg.status == "error"

    async def test_grep_tool_async_happy_path_forwards_extended_options(self, tmp_path):
        """The async tool body must thread the extended ripgrep options through to the backend's
        agrep and render a success ToolMessage — this is the actual main-agent code path."""
        from unittest.mock import AsyncMock

        from deepagents.backends.protocol import GrepResult

        from automation.agent.middlewares.file_system import (
            DAIVCompositeBackend,
            DAIVFilesystemMiddleware,
            SandboxFileBackend,
        )

        sandbox = SandboxFileBackend(client=AsyncMock())
        sandbox.bind_session("sid")
        sandbox.agrep = AsyncMock(
            return_value=GrepResult(matches=[{"path": "/workspace/repo/a.py", "line": 2, "text": "hit"}])
        )
        composite = DAIVCompositeBackend(default=sandbox, routes={}, artifacts_root="/workspace")
        daiv = DAIVFilesystemMiddleware(backend=composite)
        grep = self._grep_coroutine(daiv)

        msg = await grep(
            pattern="h.t",
            runtime=_runtime(state={}, working_dir=tmp_path),
            path="/workspace/repo",
            case_insensitive=True,
            multiline=True,
            head_limit=5,
        )

        assert msg.status == "success"
        _, kwargs = sandbox.agrep.call_args
        assert kwargs["case_insensitive"] is True
        assert kwargs["multiline"] is True
        assert kwargs["head_limit"] == 5


class TestDAIVCompositeAgrepForwarding:
    """DAIVCompositeBackend.agrep must forward the extended options to a backend that accepts them
    and drop them for one that does not (the fixed-signature disk backend)."""

    async def test_forwards_extended_options_to_sandbox_backend(self):
        from unittest.mock import AsyncMock

        from deepagents.backends.protocol import GrepResult

        from automation.agent.middlewares.file_system import DAIVCompositeBackend, SandboxFileBackend

        sandbox = SandboxFileBackend(client=AsyncMock())
        sandbox.bind_session("sid")
        sandbox.agrep = AsyncMock(return_value=GrepResult(matches=[]))
        composite = DAIVCompositeBackend(default=sandbox, routes={}, artifacts_root="/workspace")

        await composite.agrep("fo+", path="/workspace", glob="*.py", case_insensitive=True, head_limit=3)

        _, kwargs = sandbox.agrep.call_args
        assert kwargs["case_insensitive"] is True
        assert kwargs["head_limit"] == 3

    async def test_disk_backend_call_omits_extended_options(self, tmp_path):
        """The disk FilesystemBackend keeps the fixed 3-arg agrep signature, so the composite must
        not pass it the extended kwargs (which would TypeError)."""
        from automation.agent.middlewares.file_system import DAIVCompositeBackend

        (tmp_path / "a.py").write_text("foo\n")
        disk = DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        composite = DAIVCompositeBackend(default=disk, routes={})

        # Must not raise despite passing the extended options.
        result = await composite.agrep("foo", path="/", case_insensitive=True, multiline=True, head_limit=1)
        assert result.error is None

    @staticmethod
    def _sandbox_returning(paths):
        """A bound SandboxFileBackend whose agrep returns one match per path (kwargs ignored)."""
        from unittest.mock import AsyncMock

        from deepagents.backends.protocol import GrepResult

        from automation.agent.middlewares.file_system import SandboxFileBackend

        backend = SandboxFileBackend(client=AsyncMock())
        backend.bind_session("sid")
        backend.agrep = AsyncMock(return_value=GrepResult(matches=[{"path": p, "line": 1, "text": "x"} for p in paths]))
        return backend

    async def test_head_limit_caps_merged_total_across_backends(self):
        """Each sub-backend self-caps, but the aggregate (path='/') merge must re-apply head_limit so
        'at most N' doesn't silently become 'up to N per backend' once a route is present."""
        from automation.agent.middlewares.file_system import DAIVCompositeBackend

        default = self._sandbox_returning(["/a.py", "/b.py", "/c.py"])
        route = self._sandbox_returning(["/d.py", "/e.py", "/f.py"])
        composite = DAIVCompositeBackend(default=default, routes={"/skills/": route}, artifacts_root="/workspace")

        result = await composite.agrep("x", path="/", head_limit=4)

        assert result.error is None
        assert len(result.matches or []) == 4, "3 default + 3 route = 6, must be capped to 4"

    async def test_single_route_remaps_paths_and_caps(self):
        """The routed-path branch must remap matches with the route prefix AND honor head_limit
        (a disk-routed backend never receives head_limit, so the composite must cap it)."""
        from automation.agent.middlewares.file_system import DAIVCompositeBackend

        route = self._sandbox_returning(["/x1.py", "/x2.py", "/x3.py"])
        default = self._sandbox_returning([])
        composite = DAIVCompositeBackend(default=default, routes={"/skills/": route}, artifacts_root="/workspace")

        result = await composite.agrep("x", path="/skills/sub", head_limit=2)

        assert result.error is None
        assert len(result.matches or []) == 2, "route returned 3, must be capped to 2"
        assert all(m["path"].startswith("/skills") for m in result.matches or []), "routed paths must be remapped"
