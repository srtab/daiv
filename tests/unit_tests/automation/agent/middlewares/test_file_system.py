from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import FileUploadResponse
from deepagents.graph import _apply_custom_middleware
from deepagents.middleware.filesystem import FilesystemMiddleware as UpstreamFilesystemMiddleware
from deepagents.middleware.filesystem import FilesystemPermission, _check_fs_permission
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from mcp.server.mcpserver import MCPServer

from automation.agent.constants import SKILLS_TOOL_NAME
from automation.agent.mcp.client import list_tools
from automation.agent.middlewares import file_system as fs_module
from automation.agent.middlewares.file_system import (
    EDIT_SUCCESS_PREFIX,
    READ_ONLY_PERMISSIONS,
    WRITE_SUCCESS_PREFIX,
    DAIVFilesystemMiddleware,
    _file_write_confirmation,
    large_tool_results_prefix,
    write_tool_result_to_file,
)
from automation.agent.workspace.backends import DAIVFilesystemBackend, build_disk_workspace_backend
from tests.unit_tests.automation.agent.mcp.helpers import client_for, serve
from tests.unit_tests.conftest import recording_backend, unacquired_backend

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
    """Disk-backed repo backend + DAIV's filesystem middleware and its tool map."""
    backend = DAIVFilesystemBackend(root_dir=working_repo.parent, virtual_mode=True)
    fs = DAIVFilesystemMiddleware(backend=backend, custom_tool_descriptions=fs_module.CUSTOM_TOOL_DESCRIPTIONS)
    tools = {tool.name: tool for tool in fs.tools}
    return SimpleNamespace(backend=backend, tools=tools, repo=working_repo, middleware=fs)


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


def test_write_file_description_does_not_contradict_upstream():
    """DAIV's write_file guidance must not assert a mechanism upstream has changed.

    Through deepagents 0.6 the disk backend refused to overwrite, so DAIV's appended text said
    write_file "can ONLY create new files". 0.7 replaces the file instead and says so in its own
    description, which turned the appended line into a contradiction inside one tool description.
    Whether an overwrite is refused is now backend-dependent, so the guidance may only express a
    preference.
    """
    from automation.agent.middlewares.file_system import WRITE_FILE_TOOL_DESCRIPTION

    assert "edit_file" in WRITE_FILE_TOOL_DESCRIPTION, "must still steer edits to edit_file"
    assert "ONLY create new files" not in WRITE_FILE_TOOL_DESCRIPTION
    assert "will fail on files that already exist" not in WRITE_FILE_TOOL_DESCRIPTION


async def test_glob_description_does_not_claim_bare_patterns_fail(tmp_path: Path):
    """A bare repo-relative glob pattern does match, so the description must not say otherwise.

    ``CompositeBackend.aglob`` merges the default backend with every route when ``path`` is unset,
    so ``tests/**/*.py`` resolves under ``/workspace/repo`` without a ``**/`` prefix. Pin both the
    claim and the behaviour, so neither can drift back independently.
    """
    from automation.agent.constants import REPO_PATH
    from automation.agent.middlewares.file_system import GLOB_TOOL_DESCRIPTION

    assert "matches nothing under the repo" not in GLOB_TOOL_DESCRIPTION
    assert "repository root" in GLOB_TOOL_DESCRIPTION

    clone = tmp_path / "repo"
    (clone / "tests").mkdir(parents=True)
    (clone / "tests" / "test_a.py").write_text("x\n")
    backend = build_disk_workspace_backend(clone)

    bare = await backend.aglob("tests/**/*.py")
    paths = [e["path"] for e in (bare.matches or [])]

    assert f"{REPO_PATH}/tests/test_a.py" in paths, "a bare repo-relative pattern must still match"
    # Scoping to the repo root is what yields the single canonical path the description recommends.
    scoped = await backend.aglob("tests/**/*.py", path=REPO_PATH)
    assert [e["path"] for e in (scoped.matches or [])] == [f"{REPO_PATH}/tests/test_a.py"]


def test_grep_arg_schema_describes_regex(setup):
    """The grep tool's input schema must describe ``pattern`` as a regex, not literal text.

    deepagents ships ``GrepSchema`` with a "literal string, not regex" ``pattern`` description (and a
    "current working directory" ``path`` default) that the model sees in the tool's input schema
    alongside DAIV's regex-aware tool description — a direct contradiction. ``_align_arg_schema``
    rewrites both at import; pin them here so a deepagents bump that reworks GrepSchema fails loudly
    instead of silently restoring the contradiction.
    """
    props = setup.tools["grep"].args_schema.model_json_schema()["properties"]

    pattern_desc = props["pattern"]["description"]
    assert "regular expression" in pattern_desc.lower()
    assert "literal" not in pattern_desc.lower()
    assert pattern_desc == fs_module._GREP_PATTERN_ARG_DESCRIPTION
    assert props["path"]["description"] == fs_module._GREP_PATH_ARG_DESCRIPTION


def test_glob_description_steers_over_find(setup):
    """glob's own description must steer the model away from shell `find` and warn about the
    `/`-anchoring footgun, mirroring grep's 'prefer over bash' treatment."""
    desc = setup.tools["glob"].description
    low = desc.lower()
    assert "prefer this tool" in low
    assert "shell `find`" in low  # names the shell tool it replaces
    assert "**/" in desc  # the anchoring guidance the model must learn
    assert fs_module._GLOB_EXTRA in desc


def test_glob_arg_schema_warns_root_anchoring(setup):
    """deepagents ships GlobSchema with a bare `*.txt` pattern example and a "Defaults to root '/'"
    path description. `_align_arg_schema` rewrites both at import; pin them so a deepagents bump
    that reworks GlobSchema fails loudly instead of silently regressing."""
    props = setup.tools["glob"].args_schema.model_json_schema()["properties"]
    assert props["pattern"]["description"] == fs_module._GLOB_PATTERN_ARG_DESCRIPTION
    assert props["path"]["description"] == fs_module._GLOB_PATH_ARG_DESCRIPTION
    # the bare `*.txt` example (no `**/` prefix) must be replaced
    assert "*.txt" not in props["pattern"]["description"]
    # the path description must warn it is NOT the repository root
    assert "filesystem root" in props["path"]["description"].lower()


def test_ls_description_steers_over_shell_ls(setup):
    """ls's own description must steer the model away from shell `ls`, reframe it as a directory
    explorer (not only a read precursor), and state that `path` is required/absolute (shell `ls`
    defaults to cwd; the dedicated tool errors with no path)."""
    desc = setup.tools["ls"].description
    low = desc.lower()
    assert "prefer this tool" in low
    assert "shell `ls`" in low
    assert "required" in low  # the no-implicit-cwd footgun
    assert fs_module._LS_EXTRA in desc


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


class TestReadOnlyPermissions:
    def test_deny_every_write(self):
        """Locks the read-only contract of the explore subagent and the code-review detectors: relaxing this constant
        would silently grant them write capability."""
        assert [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")] == READ_ONLY_PERMISSIONS

    def test_deny_dot_paths(self):
        for path in ("/workspace/repo/.gitlab-ci.yml", "/workspace/repo/.github/workflows/ci.yml", "/a/.b/.c/d"):
            assert _check_fs_permission(READ_ONLY_PERMISSIONS, "write", path) == "deny", path


# ---------------------------------------------------------------------------
# grep output-mode legibility
# ---------------------------------------------------------------------------


def test_daiv_filesystem_middleware_replaces_upstream_in_the_merge(setup):
    """``create_deep_agent`` auto-adds its own ``FilesystemMiddleware`` and merges ours in by
    ``.name`` — which defaults to the class name. A subclass under a new name is therefore
    APPENDED rather than substituted, leaving two filesystem middlewares and restoring the
    ``delete``/``execute`` tools that ``WORKSPACE_FS_TOOLS`` deliberately withholds from the main
    agent. ``profile.py`` documents the same ``__name__``-collision requirement for the prompt-cache
    subclass.
    """
    merged = _apply_custom_middleware(
        [UpstreamFilesystemMiddleware(backend=setup.backend)], [DAIVFilesystemMiddleware(backend=setup.backend)]
    )

    assert [type(m) for m in merged] == [DAIVFilesystemMiddleware], (
        "DAIV's subclass must take upstream's slot, not sit alongside it"
    )


async def test_grep_label_does_not_disable_large_tool_result_eviction(setup):
    """Upstream's ``awrap_tool_call`` evicts oversized results for every tool outside
    ``TOOLS_EXCLUDED_FROM_EVICTION`` — bash, task, MCP. Overriding it to add the grep label must
    delegate to ``super()``, or one huge bash result blows up the context.
    """
    fs = DAIVFilesystemMiddleware(backend=setup.backend, tool_token_limit_before_evict=10)
    oversized = "x" * 20_000
    request = ToolCallRequest(
        tool_call={"name": "bash", "args": {}, "id": "call_bash"},
        tool=None,
        state={"messages": []},
        runtime=_runtime(state={}, working_dir=setup.repo),
    )

    async def handler(req: ToolCallRequest):
        return ToolMessage(content=oversized, tool_call_id=req.tool_call["id"], name="bash")

    result = await fs.awrap_tool_call(request, handler)

    assert oversized not in str(result.content), "oversized result was not evicted — super() was bypassed"


async def test_skill_results_are_never_evicted(setup):
    """A skill body is the instructions the model must follow, so it skips eviction however large it is."""
    fs = DAIVFilesystemMiddleware(backend=setup.backend, tool_token_limit_before_evict=10)
    body = "x" * 20_000
    request = ToolCallRequest(
        tool_call={"name": SKILLS_TOOL_NAME, "args": {"skill": "demo"}, "id": "call_skill"},
        tool=None,
        state={"messages": []},
        runtime=_runtime(state={}, working_dir=setup.repo),
    )

    async def handler(req: ToolCallRequest):
        return Command(update={"messages": [ToolMessage(content=body, tool_call_id="call_skill")]})

    result = await fs.awrap_tool_call(request, handler)

    assert result.update["messages"][0].content == body


async def _grep_via_middleware(setup, **kwargs) -> str:
    """Run the real grep tool through the DAIV filesystem middleware and return the text."""
    runtime = _runtime(state={}, working_dir=setup.repo)
    request = ToolCallRequest(
        tool_call={"name": "grep", "args": dict(kwargs), "id": "call_grep"},
        tool=setup.tools["grep"],
        state={"messages": []},
        runtime=runtime,
    )

    async def handler(req: ToolCallRequest):
        content = await setup.tools["grep"].coroutine(runtime=runtime, **req.tool_call["args"])
        if isinstance(content, ToolMessage):
            return content
        return ToolMessage(content=content, tool_call_id=req.tool_call["id"], name="grep")

    result = await setup.middleware.awrap_tool_call(request, handler)
    return result.content if isinstance(result, ToolMessage) else result


class TestGrepOutputMode:
    """A grep result must name the mode that produced it."""

    async def _grep(self, setup, **kwargs):
        return await _grep_via_middleware(setup, **kwargs)

    async def test_default_mode_result_names_the_mode_and_the_remedy(self, setup):
        (setup.repo / "a.py").write_text("needle here\n")

        text = await self._grep(setup, pattern="needle")

        assert text.startswith("Mode: files_with_matches")
        assert 'output_mode="content"' in text
        assert "a.py" in text

    async def test_content_mode_result_names_content_and_does_not_nag(self, setup):
        (setup.repo / "a.py").write_text("needle here\n")

        text = await self._grep(setup, pattern="needle", output_mode="content")

        assert text.startswith("Mode: content")
        assert 'output_mode="content"' not in text, "should not suggest a mode already in use"
        assert "needle here" in text

    async def test_no_match_is_not_told_to_retry_in_content_mode(self, setup):
        (setup.repo / "a.py").write_text("nothing relevant\n")

        text = await self._grep(setup, pattern="needle")

        assert 'output_mode="content"' not in text

    async def test_count_mode_result_names_count(self, setup):
        (setup.repo / "a.py").write_text("needle\nneedle\n")

        text = await self._grep(setup, pattern="needle", output_mode="count")

        assert text.startswith("Mode: count")
        assert "a.py: 2" in text


def test_regex_literal_hint_patch_is_live():
    """Catches what the behavioural test below cannot: if upstream renames the symbol AND rewords
    the hint in one bump, the patch targets a dead attribute and the reworded hint reaches the
    model with both assertions still green.
    """
    assert fs_module._REGEX_HINT_PATCH_APPLIED, "upstream no longer exposes regex_literal_hint here"


async def test_no_match_does_not_claim_grep_is_literal(setup):
    """deepagents appends a "grep matches literal text, not regex" note on a zero-match
    regex-looking pattern, telling the caller to "run a separate search per alternative".

    Both DAIV backends grep by regex, so the note is false here and drives alternation fan-out.
    """
    (setup.repo / "a.py").write_text("nothing relevant\n")

    text = await _grep_via_middleware(setup, pattern="alpha|beta")

    assert "literal text, not regex" not in text
    assert "separate search per alternative" not in text


def test_grep_description_states_the_default_output_mode():
    """The tool description must say what a bare ``grep(pattern=...)`` returns."""
    desc = fs_module.GREP_TOOL_DESCRIPTION

    assert "files_with_matches" in desc, "description must name the default output mode"
    assert 'output_mode="content"' in desc


@pytest.mark.parametrize(
    ("artifacts_root", "expected"),
    [("/workspace", "/workspace/large_tool_results"), (None, "/large_tool_results")],
    ids=["composite", "bare"],
)
def test_large_tool_results_prefix_is_where_upstream_evicts(artifacts_root, expected):
    """A bare backend carries no artifacts_root the middleware would honour, so it falls back to "/"."""
    if artifacts_root is None:
        backend = unacquired_backend()
    else:
        backend = CompositeBackend(default=unacquired_backend(), routes={}, artifacts_root=artifacts_root)

    assert large_tool_results_prefix(backend) == expected
    assert UpstreamFilesystemMiddleware(backend=backend)._large_tool_results_prefix == expected


def test_file_write_confirmation_shape():
    output = "line1\nline2\nline3"
    msg = _file_write_confirmation("/workspace/large_tool_results/x", 17, 3, output)
    assert "/workspace/large_tool_results/x" in msg
    assert "17 bytes" in msg
    assert "3 lines" in msg
    assert "line1" in msg  # head preview included


def test_file_write_confirmation_caps_preview_to_25_lines():
    output = "\n".join(f"line{i}" for i in range(100))
    msg = _file_write_confirmation("/workspace/large_tool_results/x", 999, 100, output)
    assert "line24" in msg  # 25th line (0-indexed) is shown
    assert "line25" not in msg  # 26th line is not


def test_file_write_confirmation_caps_preview_chars():
    output = "x" * 5000  # single very long line
    msg = _file_write_confirmation("/workspace/large_tool_results/x", 5000, 1, output)
    assert "(preview truncated)" in msg
    assert len(msg) < 5000


async def test_write_tool_result_to_file_writes_via_backend_and_confirms():
    backend = recording_backend()

    result = await write_tool_result_to_file(
        "a\nb\nc", runtime=SimpleNamespace(tool_call_id="call_1"), backend=backend, tool_name="gitlab"
    )

    path, content = backend.awrite.call_args.args
    # path is keyed by tool_call_id, exactly like the middleware's auto-eviction
    assert path == "/workspace/large_tool_results/call_1"
    assert content == "a\nb\nc"  # full content, untruncated
    assert result.startswith("Wrote ")
    assert "3 lines" in result


async def test_write_tool_result_to_file_returns_error_on_backend_failure():
    result = await write_tool_result_to_file(
        "x",
        runtime=SimpleNamespace(tool_call_id="call_1"),
        backend=recording_backend(error="disk full"),
        tool_name="gitlab",
    )

    assert result.startswith("error:")
    assert "disk full" in result


async def test_write_tool_result_to_file_returns_error_when_backend_raises():
    """A raised exception from ``awrite`` must be caught and returned as an ``error:`` string,
    not propagated out of the tool (the agent's only channel is the returned string)."""
    backend = recording_backend()
    backend.awrite = AsyncMock(side_effect=RuntimeError("boom"))

    result = await write_tool_result_to_file(
        "x", runtime=SimpleNamespace(tool_call_id="call_1"), backend=backend, tool_name="gitlab"
    )

    assert result.startswith("error:")
    assert "boom" in result


async def test_write_tool_result_to_file_fails_loudly_when_tool_call_id_missing():
    """Without a tool_call_id the path key would collapse onto a shared filename, silently
    overwriting a prior dump — so the write must be refused with an ``error:`` string instead."""
    backend = recording_backend()

    result = await write_tool_result_to_file(
        "x", runtime=SimpleNamespace(tool_call_id=None), backend=backend, tool_name="gitlab"
    )

    assert result.startswith("error:")
    assert "tool_call_id" in result
    backend.awrite.assert_not_called()


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 400


def _json_with_file(data: bytes) -> str:
    return json.dumps({"id": 7, "name": "screenshot.png", "body": base64.encodebytes(data).decode()})


def _server_returning(payload: str) -> MCPServer:
    server = MCPServer(name="files-test")

    @server.tool()
    def fetch(item_id: int) -> str:
        """Fetch an item."""
        return payload

    return server


class TestEmbeddedMcpFiles:
    """An MCP result can carry a file as base64 text, which the model can neither view nor read back."""

    async def _call(self, setup, payload: str, fs: DAIVFilesystemMiddleware | None = None) -> ToolMessage:
        async with serve(_server_returning(payload)) as gate:
            (tool,) = await list_tools(client_for(gate))
            request = ToolCallRequest(
                tool_call={"name": tool.name, "args": {"item_id": 1}, "id": "call_1", "type": "tool_call"},
                tool=tool,
                state={"messages": []},
                runtime=_runtime(state={}, working_dir=setup.repo),
            )
            return await (fs or setup.middleware).awrap_tool_call(request, lambda req: tool.ainvoke(req.tool_call))

    async def test_file_is_saved_beside_evicted_results(self, setup):
        result = await self._call(setup, _json_with_file(PNG[:300]))

        path = f"{large_tool_results_prefix(setup.backend)}/call_1-1.png"
        assert path in result.text
        assert base64.b64encode(PNG[:300]).decode()[:40] not in result.text
        assert (await setup.backend.adownload_files([path]))[0].content == PNG[:300]

    async def test_large_file_no_longer_forces_eviction(self, setup):
        fs = DAIVFilesystemMiddleware(backend=setup.backend, tool_token_limit_before_evict=20_000)
        assert len(_json_with_file(PNG)) > 80_000

        result = await self._call(setup, _json_with_file(PNG), fs)

        assert f"{large_tool_results_prefix(setup.backend)}/call_1-1.png" in result.text
        assert len(result.text) < 1_000

    async def test_failed_upload_keeps_the_result_as_it_was(self, setup, monkeypatch):
        failed = AsyncMock(return_value=[FileUploadResponse(path="x", error="permission_denied")])
        monkeypatch.setattr(setup.backend, "aupload_files", failed)
        payload = _json_with_file(PNG[:300])

        result = await self._call(setup, payload)

        assert result.text == payload

    async def test_non_mcp_tool_results_are_left_alone(self, setup):
        payload = _json_with_file(PNG[:300])
        request = ToolCallRequest(
            tool_call={"name": "bash", "args": {}, "id": "call_bash"},
            tool=None,
            state={"messages": []},
            runtime=_runtime(state={}, working_dir=setup.repo),
        )

        async def handler(req: ToolCallRequest):
            return ToolMessage(content=payload, tool_call_id="call_bash", name="bash")

        result = await setup.middleware.awrap_tool_call(request, handler)

        assert result.content == payload
