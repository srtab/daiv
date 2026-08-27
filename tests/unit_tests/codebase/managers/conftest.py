"""Shared scaffolding for the manager tests.

``BaseManager.__init__`` builds a ``RepoClient`` and a store, so every test that drives a manager
method has to stub it out. One stub here rather than one per module, so a new ``__init__``
dependency is a single edit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest

from codebase.managers.base import BaseManager


@pytest.fixture
def stub_base_init():
    def _init(self, *, runtime_ctx, thread_id):
        self.ctx = runtime_ctx
        self.thread_id = thread_id
        self.client = MagicMock()
        self.store = MagicMock()
        self.git_manager = MagicMock()

    with patch.object(BaseManager, "__init__", _init):
        yield


@pytest.fixture
def noop_checkpointer():
    @asynccontextmanager
    async def _open():
        yield MagicMock()

    return _open
