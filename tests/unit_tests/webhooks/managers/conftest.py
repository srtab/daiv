"""Shared scaffolding for the manager tests.

``BaseManager.__init__`` builds a ``RepoClient``, so every test that drives a manager stubs it out. One stub here
rather than one per module, so a new ``__init__`` dependency is a single edit.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sessions.executor.lock import NoLock
from webhooks.managers.base import BaseManager

from codebase.base import GitPlatform
from tests.unit_tests.sessions.executor.conftest import agent_stack


def stub_client() -> MagicMock:
    """A ``RepoClient`` stand-in on GitLab whose bot is ``daiv-bot``."""
    client = MagicMock()
    client.git_platform = GitPlatform.GITLAB
    client.current_user.username = "daiv-bot"
    return client


def _stub_init(client: MagicMock):
    def _init(self, *, repo_id, thread_id, mention_comment_id=None):
        self.repo_id = repo_id
        self.thread_id = thread_id
        self.mention_comment_id = mention_comment_id
        self.client = client

    return _init


@pytest.fixture
def stub_base_init():
    with patch.object(BaseManager, "__init__", _stub_init(stub_client())):
        yield


@pytest.fixture
def captured_client():
    """Stub ``BaseManager.__init__`` so every manager instance shares one client mock the test can inspect."""
    client = stub_client()
    with patch.object(BaseManager, "__init__", _stub_init(client)):
        yield client


def clone_raising(exc: Exception) -> MagicMock:
    """A ``set_runtime_ctx`` stand-in whose clone fails with ``exc``."""
    entered = MagicMock()
    entered.__aenter__ = AsyncMock(side_effect=exc)
    return MagicMock(return_value=entered)


def addressor_agent(*, state_values: dict | None = None, **ainvoke) -> MagicMock:
    """An agent whose ``ainvoke`` is an ``AsyncMock(**ainvoke)`` and whose checkpoint holds ``state_values``."""
    agent = MagicMock()
    agent.get_name.return_value = "daiv"
    agent.ainvoke = AsyncMock(**ainvoke)
    agent.aget_state = AsyncMock(return_value=SimpleNamespace(values=state_values or {}))
    return agent


@contextmanager
def addressor_run(
    agent: MagicMock,
    *,
    draft_published: bool = False,
    kwargs_error: Exception | None = None,
    stub_recovery: bool = True,
    real_lock: bool = False,
    ctx=None,
    context=None,
    resolve=None,
):
    """Stub the executor around ``agent`` for one manager run; yield the ``agent_stack`` namespace plus ``recover``.

    ``stub_recovery=False`` keeps the real draft recovery (``recover`` is then ``None``). ``real_lock`` keeps the
    real session-row check and lock; otherwise the run is unlocked and needs no database.
    """
    resolve = resolve or MagicMock(
        return_value={"model_names": ["m"], "thinking_level": "medium"}, side_effect=kwargs_error
    )
    recovery = (
        patch("sessions.executor.run.recover_draft", AsyncMock(return_value=draft_published))
        if stub_recovery
        else nullcontext()
    )
    with (
        agent_stack(agent, ctx=ctx, context=context, resolve=resolve) as stack,
        nullcontext() if real_lock else patch.object(BaseManager, "_lock_policy", AsyncMock(return_value=NoLock())),
        recovery as recover,
    ):
        stack.recover = recover
        yield stack
