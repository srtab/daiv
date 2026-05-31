from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langgraph.types import Command

from automation.agent.tools.git_publish import commit_changes, create_merge_request
from codebase.base import GitPlatform
from codebase.utils import GitManager
from core.constants import BOT_LABEL
from core.sandbox.schemas import RunCommandResult, RunCommandsResponse


class _FakeClient:
    """Records issued git commands; canned ``(exit_code, output)`` per command substring."""

    def __init__(self, responses: dict[str, tuple[int, str]] | None = None) -> None:
        self.responses = responses or {}
        self.commands: list[str] = []

    async def run_commands(self, session_id, request) -> RunCommandsResponse:  # noqa: ARG002
        command = request.commands[0]
        self.commands.append(command)
        code, out = 0, ""
        for needle, (c, o) in self.responses.items():
            if needle in command:
                code, out = c, o
                break
        return RunCommandsResponse(results=[RunCommandResult(command=command, output=out, exit_code=code)], patch=None)

    def ran(self, needle: str) -> bool:
        return any(needle in command for command in self.commands)


def _runtime(*, merge_request=None) -> SimpleNamespace:
    ctx = SimpleNamespace(
        repository=SimpleNamespace(slug="group/repo"),
        config=SimpleNamespace(default_branch="main"),
        issue=None,
        git_platform=GitPlatform.GITLAB,
        bot_username="daiv-bot",
        gitrepo=None,
    )
    return SimpleNamespace(
        state={"session_id": "sid", "merge_request": merge_request}, context=ctx, tool_call_id="call_1"
    )


@pytest.fixture
def patch_git_manager(monkeypatch):
    """Patch ``open_git_manager`` to yield a sandbox-mode GitManager over a fake client."""

    def _install(responses: dict[str, tuple[int, str]] | None = None) -> _FakeClient:
        client = _FakeClient(responses)

        @asynccontextmanager
        async def _fake_open(*, session_id, gitrepo):  # noqa: ARG001
            yield GitManager(client=client, session_id="sid")

        monkeypatch.setattr("automation.agent.tools.git_publish.open_git_manager", _fake_open)
        return client

    return _install


# ---------------------------------------------------------------------------
# commit_changes
# ---------------------------------------------------------------------------


async def test_commit_changes_runs_sandbox_commit(patch_git_manager):
    client = patch_git_manager({"status --porcelain": (0, " M a.py\n")})

    result = await commit_changes.coroutine(message="fix: thing", runtime=_runtime())

    assert isinstance(result, Command)
    assert result.update["code_changes"] is True
    assert client.ran("git -C /workspace/repo add -A")
    assert client.ran("commit -m 'fix: thing'")


async def test_commit_changes_noop_on_clean_tree(patch_git_manager):
    client = patch_git_manager({"status --porcelain": (0, "")})

    result = await commit_changes.coroutine(message="fix: thing", runtime=_runtime())

    assert isinstance(result, str)
    assert "nothing to commit" in result.lower()
    assert not client.ran("commit -m")


# ---------------------------------------------------------------------------
# create_merge_request
# ---------------------------------------------------------------------------


async def test_create_merge_request_pushes_then_calls_platform(patch_git_manager, monkeypatch):
    client = patch_git_manager({"status --porcelain": (0, ""), "ls-remote": (0, "")})

    mr = SimpleNamespace(web_url="https://example/mr/1", source_branch="fix-thing", merge_request_id=1)
    platform = MagicMock()
    platform.update_or_create_merge_request = MagicMock(return_value=mr)
    monkeypatch.setattr(
        "automation.agent.tools.git_publish.RepoClient.create_instance", MagicMock(return_value=platform)
    )

    result = await create_merge_request.coroutine(
        title="Fix thing", description="body", branch="fix-thing", runtime=_runtime()
    )

    assert isinstance(result, Command)
    assert result.update["merge_request"] is mr
    assert result.update["code_changes"] is True
    assert client.ran("push origin HEAD:fix-thing")

    platform.update_or_create_merge_request.assert_called_once()
    kwargs = platform.update_or_create_merge_request.call_args.kwargs
    assert kwargs["source_branch"] == "fix-thing"
    assert kwargs["target_branch"] == "main"
    assert kwargs["title"] == "Fix thing"
    assert kwargs["labels"] == [BOT_LABEL]


async def test_create_merge_request_reuses_existing_mr_branch(patch_git_manager, monkeypatch):
    client = patch_git_manager({"status --porcelain": (0, "")})

    existing = SimpleNamespace(source_branch="existing-branch", merge_request_id=7)
    mr = SimpleNamespace(web_url="https://example/mr/7", source_branch="existing-branch", merge_request_id=7)
    platform = MagicMock()
    platform.update_or_create_merge_request = MagicMock(return_value=mr)
    monkeypatch.setattr(
        "automation.agent.tools.git_publish.RepoClient.create_instance", MagicMock(return_value=platform)
    )

    result = await create_merge_request.coroutine(
        title="Follow-up", description="more", runtime=_runtime(merge_request=existing)
    )

    assert isinstance(result, Command)
    # Pushes onto the existing MR's branch; no ls-remote uniqueness probe needed.
    assert client.ran("push origin HEAD:existing-branch")
    assert not client.ran("ls-remote")
    assert platform.update_or_create_merge_request.call_args.kwargs["source_branch"] == "existing-branch"


async def test_create_merge_request_commits_leftover_changes(patch_git_manager, monkeypatch):
    """Uncommitted work at MR time is folded in (committed with the title) so nothing is lost."""
    client = patch_git_manager({"status --porcelain": (0, " M leftover.py\n"), "ls-remote": (0, "")})

    mr = SimpleNamespace(web_url="https://example/mr/2", source_branch="add-thing", merge_request_id=2)
    platform = MagicMock()
    platform.update_or_create_merge_request = MagicMock(return_value=mr)
    monkeypatch.setattr(
        "automation.agent.tools.git_publish.RepoClient.create_instance", MagicMock(return_value=platform)
    )

    await create_merge_request.coroutine(title="Add thing", description="body", runtime=_runtime())

    assert client.ran("git -C /workspace/repo add -A")
    assert client.ran("commit -m 'Add thing'")
    assert client.ran("push origin HEAD:add-thing")
