"""Shared fixtures for chat API tests."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


@pytest.fixture
def captured_runs():
    """Patch ``supervisor.spawn`` so view tests can deterministically await the
    spawned run: tasks are real (created on the test loop) and collected here —
    ``await asyncio.gather(*captured_runs)`` drives them to completion before
    DB/relay assertions.
    """
    from chat.api import runner

    tasks: list[asyncio.Task] = []

    def _spawn(streamer):
        task = asyncio.get_running_loop().create_task(runner.run_to_relay(streamer))
        tasks.append(task)
        return task

    with patch("chat.api.runner.supervisor.spawn", side_effect=_spawn):
        yield tasks
