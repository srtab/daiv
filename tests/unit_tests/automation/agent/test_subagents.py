"""Tests for deepagent subagents.

After migrating to ``create_deep_agent``, the public factories return
``CompiledSubAgent`` dicts (``{name, description, runnable}``) — the middleware
stack is baked into the runnable. Middleware-composition tests therefore
exercise ``_build_general_purpose_middleware`` directly rather than introspect
the compiled runnable, which keeps coverage focused on DAIV's choices about
which middlewares to compose.
"""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import ModelFallbackMiddleware

from automation.agent.middlewares.git_platform import GitPlatformMiddleware
from automation.agent.middlewares.sandbox import SandboxMiddleware
from automation.agent.middlewares.web_fetch import WebFetchMiddleware
from automation.agent.middlewares.web_search import WebSearchMiddleware
from automation.agent.subagents import (
    _build_general_purpose_middleware,
    create_explore_subagent,
    create_general_purpose_subagent,
    load_custom_subagents,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestGeneralPurposeMiddleware:
    """Tests for ``_build_general_purpose_middleware`` — the middleware composer."""

    @pytest.fixture
    def mock_backend(self):
        return Mock()

    @pytest.fixture
    def mock_model(self):
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        ctx = Mock()
        ctx.gitrepo.working_dir = str(repo_dir)
        return ctx

    def test_includes_full_stack_by_default(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=True,
            web_fetch_enabled=True,
        )
        assert any(isinstance(m, FilesystemMiddleware) for m in middleware)
        assert any(isinstance(m, GitPlatformMiddleware) for m in middleware)
        assert any(isinstance(m, WebFetchMiddleware) for m in middleware)
        assert any(isinstance(m, WebSearchMiddleware) for m in middleware)
        sandbox_middlewares = [m for m in middleware if isinstance(m, SandboxMiddleware)]
        assert len(sandbox_middlewares) == 1
        assert sandbox_middlewares[0].close_session is False

    def test_excludes_sandbox_when_disabled(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=False,
            web_search_enabled=True,
            web_fetch_enabled=True,
        )
        assert not any(isinstance(m, SandboxMiddleware) for m in middleware)

    def test_excludes_web_search_middleware(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=False,
            web_fetch_enabled=True,
        )
        assert not any(isinstance(m, WebSearchMiddleware) for m in middleware)

    def test_excludes_web_fetch_middleware(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=True,
            web_fetch_enabled=False,
        )
        assert not any(isinstance(m, WebFetchMiddleware) for m in middleware)

    def test_includes_fallback_middleware_when_fallback_models_provided(
        self, mock_model, mock_backend, mock_runtime_ctx
    ):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=True,
            web_fetch_enabled=True,
            fallback_models=[Mock(), Mock()],
        )
        assert any(isinstance(m, ModelFallbackMiddleware) for m in middleware)

    def test_excludes_fallback_middleware_when_no_fallback_models(self, mock_model, mock_backend, mock_runtime_ctx):
        middleware = _build_general_purpose_middleware(
            mock_model,
            mock_backend,
            mock_runtime_ctx,
            sandbox_enabled=True,
            web_search_enabled=True,
            web_fetch_enabled=True,
        )
        assert not any(isinstance(m, ModelFallbackMiddleware) for m in middleware)

    async def test_subagent_write_file_uses_parent_session_id(self, tmp_path, mock_model):
        """The subagent's sync-aware write_file must thread the parent's session_id into the sandbox call.

        DAIV-custom contract: a subagent inheriting the parent runtime's ``session_id`` calls the
        sandbox under that same session, never opening a fresh one. Exercises the production wrap
        path: ``_build_general_purpose_middleware`` → ``SandboxMiddleware.awrap_model_call`` →
        wrapped ``write_file``.
        """
        from types import SimpleNamespace

        from deepagents.backends.filesystem import FilesystemBackend

        from automation.agent.middlewares.file_system import SandboxSyncer
        from core.sandbox.schemas import ApplyMutationsResponse, MutationResult

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        ctx = Mock()
        ctx.gitrepo.working_dir = str(repo_dir)

        fake_client = AsyncMock()
        fake_client.apply_file_mutations.return_value = ApplyMutationsResponse(
            results=[MutationResult(path="/repo/sub.py", ok=True, error=None)]
        )

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        middleware = _build_general_purpose_middleware(
            mock_model, backend, ctx, sandbox_enabled=True, web_search_enabled=False, web_fetch_enabled=False
        )

        fs_mw = next(m for m in middleware if isinstance(m, FilesystemMiddleware))
        sandbox_mw = next(m for m in middleware if isinstance(m, SandboxMiddleware))
        # Inject fake client + syncer to skip network setup that abefore_agent would otherwise drive.
        sandbox_mw._client = fake_client
        sandbox_mw._syncer = SandboxSyncer(backend=backend, working_dir=repo_dir, client=fake_client)

        captured = {}

        async def handler(req):
            captured["tools"] = list(req.tools)
            return object()

        request = SimpleNamespace(
            tools=list(fs_mw.tools),
            system_prompt="base prompt",
            override=lambda **kw: SimpleNamespace(tools=kw["tools"], system_prompt=kw["system_prompt"]),
        )
        await sandbox_mw.awrap_model_call(request, handler)
        write = next(t for t in captured["tools"] if t.name == "write_file")

        runtime = SimpleNamespace(
            state={"session_id": "parent-sid"},
            context=SimpleNamespace(gitrepo=SimpleNamespace(working_dir=str(repo_dir))),
        )
        result = await write.coroutine(file_path=f"/{repo_dir.name}/sub.py", content="x", runtime=runtime)

        assert "Updated file" in result
        fake_client.apply_file_mutations.assert_awaited_once()
        call_session_id = fake_client.apply_file_mutations.call_args.args[0]
        assert call_session_id == "parent-sid"


class TestGeneralPurposeSubagent:
    """Tests for the public ``create_general_purpose_subagent`` factory."""

    @pytest.fixture
    def mock_backend(self):
        return Mock()

    @pytest.fixture
    def mock_model(self):
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        ctx = Mock()
        ctx.gitrepo.working_dir = str(repo_dir)
        return ctx

    def test_returns_compiled_subagent(self, mock_model, mock_backend, mock_runtime_ctx):
        result = create_general_purpose_subagent(mock_model, mock_backend, mock_runtime_ctx)

        assert isinstance(result, dict)
        assert result["name"] == "general-purpose"
        assert result["description"]
        assert "runnable" in result


class TestExploreSubagent:
    """Tests for the public ``create_explore_subagent`` factory."""

    def test_returns_compiled_subagent(self):
        result = create_explore_subagent(Mock())

        assert isinstance(result, dict)
        assert result["name"] == "explore"
        assert result["description"]
        assert "runnable" in result

    def test_read_only_guard_raises_when_write_tools_missing(self, tmp_path, monkeypatch):
        """Regression: if upstream renames ``write_file``/``edit_file``, the read-only filter
        would silently match nothing and the explore subagent would regain write capability —
        a security contract. ``_build_read_only_filesystem_middleware`` must fail loud instead.
        """
        from deepagents.backends.filesystem import FilesystemBackend

        from automation.agent.subagents import _build_read_only_filesystem_middleware

        # Pretend upstream renamed ``write_file`` so it is no longer in the produced tool list.
        monkeypatch.setattr("automation.agent.subagents.WRITE_TOOL_NAMES", frozenset({"unmapped_write_tool"}))

        backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
        with pytest.raises(RuntimeError, match=r"no longer exposes expected write tools"):
            _build_read_only_filesystem_middleware(backend)


def _make_subagent_md(*, name: str, description: str, model: str | None = None, body: str = "You are a custom agent."):
    lines = ["---", f"name: {name}", f"description: {description}"]
    if model:
        lines.append(f"model: {model}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


class TestCustomSubagents:
    """Tests for load_custom_subagents."""

    @pytest.fixture
    def mock_model(self):
        return Mock()

    @pytest.fixture
    def mock_runtime_ctx(self, tmp_path):
        ctx = Mock()
        ctx.gitrepo.working_dir = str(tmp_path / "repo")
        return ctx

    async def test_loads_custom_subagent(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(
            _make_subagent_md(name="my-agent", description="Does custom things", body="You do custom things.")
        )

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 1
        assert result[0]["name"] == "my-agent"
        assert result[0]["description"] == "Does custom things"
        assert "runnable" in result[0]

    async def test_loads_multiple_subagents(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "agent-a.md").write_text(_make_subagent_md(name="agent-a", description="Agent A"))
        (subagents_dir / "agent-b.md").write_text(_make_subagent_md(name="agent-b", description="Agent B"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        names = {s["name"] for s in result}
        assert names == {"agent-a", "agent-b"}

    async def test_skips_non_md_files(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(_make_subagent_md(name="my-agent", description="Does things"))
        (subagents_dir / "readme.txt").write_text("Not a subagent")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 1
        assert result[0]["name"] == "my-agent"

    async def test_skips_directories(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "my-agent.md").write_text(_make_subagent_md(name="my-agent", description="Does things"))
        (subagents_dir / "some-dir").mkdir()

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 1
        assert result[0]["name"] == "my-agent"

    async def test_skips_missing_name(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "bad.md").write_text("---\ndescription: no name\n---\nBody here.")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 0

    async def test_skips_missing_description(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "bad.md").write_text("---\nname: bad\n---\nBody here.")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 0

    async def test_skips_empty_body(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "empty.md").write_text("---\nname: empty\ndescription: empty body\n---\n")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 0

    async def test_skips_no_frontmatter(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "plain.md").write_text("Just some markdown without frontmatter.")

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert len(result) == 0

    async def test_returns_empty_when_no_source_exists(self, mock_model, mock_runtime_ctx):
        backend = Mock()
        backend.als = AsyncMock(side_effect=FileNotFoundError("not found"))

        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        assert result == []

    @pytest.mark.parametrize("reserved_name", ["general-purpose", "explore"])
    async def test_skips_builtin_name_collision(self, tmp_path: Path, mock_model, mock_runtime_ctx, reserved_name):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / f"{reserved_name}.md").write_text(
            _make_subagent_md(name=reserved_name, description="Trying to override a built-in subagent")
        )
        (subagents_dir / "custom.md").write_text(_make_subagent_md(name="custom", description="Custom agent"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        names = {s["name"] for s in result}
        assert reserved_name not in names
        assert "custom" in names

    async def test_skips_invalid_model(self, tmp_path: Path, mock_model, mock_runtime_ctx):
        from deepagents.backends.filesystem import FilesystemBackend

        subagents_dir = tmp_path / "repo" / ".agents" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "bad-model.md").write_text(
            _make_subagent_md(name="bad-model", description="Has invalid model", model="totally-invalid-model")
        )
        (subagents_dir / "good.md").write_text(_make_subagent_md(name="good", description="Good agent"))

        backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
        result = await load_custom_subagents(
            model=mock_model, backend=backend, runtime=mock_runtime_ctx, sources=["/repo/.agents/subagents"]
        )

        names = {s["name"] for s in result}
        assert "bad-model" not in names
        assert "good" in names
