"""Shared scaffolding for the manager tests.

``BaseManager.__init__`` builds a ``RepoClient`` and a store, so every test that drives a manager
method has to stub it out. One stub here rather than one per module, so a new ``__init__``
dependency is a single edit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager, nullcontext
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase.managers.base import BaseManager

if TYPE_CHECKING:
    from codebase.base import MergeRequest


def _stub_init(client: MagicMock | None = None):
    def _init(self, *, runtime_ctx, thread_id):
        self.ctx = runtime_ctx
        self.thread_id = thread_id
        self.client = client if client is not None else MagicMock()
        self.store = MagicMock()
        self.git_manager = MagicMock()

    return _init


@pytest.fixture
def stub_base_init():
    with patch.object(BaseManager, "__init__", _stub_init()):
        yield


@asynccontextmanager
async def open_noop_checkpointer():
    yield MagicMock()


@pytest.fixture
def noop_checkpointer():
    return open_noop_checkpointer


@pytest.fixture
def captured_client():
    """Stub ``BaseManager.__init__`` so every manager instance shares one client mock the test can inspect."""
    client = MagicMock()
    with patch.object(BaseManager, "__init__", _stub_init(client)):
        yield client


def publisher_through_backend(created: list, *, publishes: MergeRequest):
    """A ``GitChangePublisher`` stand-in that pushes through whatever backend it is handed."""

    class _Publisher:
        def __init__(self, ctx, *, sandbox_backend, thread_id):
            self.sandbox_backend = sandbox_backend
            created.append(self)

        async def publish(self, *, merge_request: MergeRequest | None, as_draft: bool):
            self.target = (merge_request, as_draft)
            await self.sandbox_backend.run_commands(["git push origin HEAD"], fail_fast=True)
            return SimpleNamespace(merge_request=publishes, protected_branch_fallback_source=None)

    return _Publisher


def addressor_agent(*, state_values: dict | None = None, **ainvoke) -> MagicMock:
    """An agent whose ``ainvoke`` is an ``AsyncMock(**ainvoke)`` and whose checkpoint holds ``state_values``."""
    agent = MagicMock()
    agent.get_name.return_value = "daiv"
    agent.ainvoke = AsyncMock(**ainvoke)
    agent.aget_state = AsyncMock(return_value=SimpleNamespace(values=state_values or {}))
    return agent


@contextmanager
def addressor_run(
    manager_cls: type[BaseManager],
    agent: MagicMock,
    *,
    draft_published: bool = False,
    kwargs_error: Exception | None = None,
    stub_recovery: bool = True,
):
    """Stub everything around ``agent`` in a ``manager_cls`` run; yield the ``create`` and ``recover`` mocks.

    ``stub_recovery=False`` keeps the real draft recovery, and ``recover`` is then ``None``.
    """
    module = manager_cls.__module__
    recovery = (
        patch.object(manager_cls, "_recover_draft", AsyncMock(return_value=draft_published))
        if stub_recovery
        else nullcontext()
    )
    with (
        patch(f"{module}.open_checkpointer", open_noop_checkpointer),
        patch(
            f"{module}.get_daiv_agent_kwargs",
            return_value={"model_names": ["m"], "thinking_level": "medium"},
            side_effect=kwargs_error,
        ),
        patch(f"{module}.create_daiv_agent", AsyncMock(return_value=agent)) as create,
        patch(f"{module}.build_langsmith_config", return_value={}),
        patch(f"{module}.track_usage_metadata", MagicMock()),
        recovery as recover,
        patch.object(BaseManager, "_build_agent_result", AsyncMock(return_value={})),
    ):
        yield SimpleNamespace(create=create, recover=recover)
