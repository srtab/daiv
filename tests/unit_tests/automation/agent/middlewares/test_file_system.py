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

    async def test_agrep_hints_on_regexy_zero_match(self, tmp_path: Path):
        """A zero-match aggregate for a regex-shaped pattern (the backends grep literally)
        returns an explicit literal-semantics hint instead of a bare "no matches" the model
        would misread as "the symbol does not exist"."""
        composite, _skills, _repo, skills_root, _repo_root = self._make_composite(tmp_path)
        (skills_root / "a.md").write_text("nothing relevant\n")

        result = await composite.agrep("get_catalog|list_relations")

        assert result.error is not None
        assert "LITERAL" in result.error
        assert "get_catalog|list_relations" in result.error

    async def test_agrep_literal_zero_match_stays_clean(self, tmp_path: Path):
        """Bare parens/dots are common in genuinely literal code searches; a zero-match for
        them is a real answer, not a hint trigger."""
        composite, _skills, _repo, _skills_root, _repo_root = self._make_composite(tmp_path)

        for pattern in ("plainmissing", "def __init__(self):"):
            result = await composite.agrep(pattern)
            assert result.error is None, pattern
            assert not result.matches, pattern

    async def test_agrep_routed_matches_survive_regexy_pattern(self, tmp_path: Path):
        """The hint must fire on the *aggregate*, never per-backend: a routed backend's real
        matches must come back even when other backends found nothing for a regexy pattern —
        a per-backend hint would abort the composite's aggregation and suppress them."""
        composite, _skills, _repo, skills_root, _repo_root = self._make_composite(tmp_path)
        (skills_root / "doc.md").write_text("literally contains foo|bar here\n")

        result = await composite.agrep("foo|bar")

        assert result.error is None
        assert result.matches

    async def test_agrep_subbackend_error_wins_over_hint(self, tmp_path: Path):
        """A genuine backend failure (grep never ran) must pass through verbatim — masking it
        with the no-matches hint would tell the model the symbol doesn't exist."""
        from deepagents.backends.protocol import GrepResult

        composite, _skills, _repo, _skills_root, _repo_root = self._make_composite(tmp_path)

        async def failing_agrep(*args, **kwargs):
            return GrepResult(error="grep failed")

        composite.default.agrep = failing_agrep

        result = await composite.agrep("foo|bar")

        assert result.error == "grep failed"
        assert "LITERAL" not in result.error

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
