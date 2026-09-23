"""Shared scaffolding for the manager tests.

``BaseManager.__init__`` builds a ``RepoClient`` and a store, so every test that drives a manager
method has to stub it out. One stub here rather than one per module, so a new ``__init__``
dependency is a single edit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

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


@pytest.fixture
def noop_checkpointer():
    @asynccontextmanager
    async def _open():
        yield MagicMock()

    return _open


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
