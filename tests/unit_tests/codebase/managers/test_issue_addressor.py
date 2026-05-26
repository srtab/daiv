"""Regression guard: the webhook entry point for an issue with the ``daiv-max`` label must
route through ``get_daiv_agent_kwargs(..., use_max=True)`` and resolve to the
``site_settings.agent_max_*`` values.

Webhook handlers bypass ``run_job_task`` and call ``create_daiv_agent`` directly via
``get_daiv_agent_kwargs``. ``use_max`` is the only remaining call-site flag after the
model-override refactor, so we lock the contract at the ``create_daiv_agent`` boundary:
the resolved primary model and thinking level must match site settings whenever the
``daiv-max`` label is present.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codebase.base import GitPlatform, Issue, User
from codebase.managers.base import BaseManager
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings

_AUTHOR = User(id=1, username="alice")


class _StubRepo:
    slug = "owner/repo"


def _ctx() -> SimpleNamespace:
    """Minimal RuntimeCtx stub: only the attributes ``_address_issue`` actually touches."""
    return SimpleNamespace(
        repository=_StubRepo(), git_platform=GitPlatform.GITLAB, bot_username="daiv-bot", config=RepositoryConfig()
    )


def _issue(*, labels: list[str]) -> Issue:
    return Issue(id=1, iid=42, title="t", author=_AUTHOR, labels=labels)


@pytest.fixture
def stub_base_init():
    """Skip BaseManager side effects (RepoClient, GitManager); we exercise ``_address_issue``
    only up to the ``create_daiv_agent`` boundary, so a stub client and store are enough."""

    def _init(self, *, runtime_ctx):
        self.ctx = runtime_ctx
        self.client = MagicMock()
        self.store = MagicMock()
        self.git_manager = MagicMock()

    with patch.object(BaseManager, "__init__", _init):
        yield


@asynccontextmanager
async def _noop_checkpointer():
    yield MagicMock()


class _CapturedError(RuntimeError):
    """Sentinel raised by the patched ``create_daiv_agent`` to short-circuit ``_address_issue``
    immediately after kwargs resolution, without exercising the full agent invocation path."""


async def _run_addressor(*, labels: list[str]) -> dict:
    """Drive ``_address_issue`` far enough to capture the resolved ``create_daiv_agent`` kwargs.

    Patches the checkpointer context manager and ``create_daiv_agent`` (the boundary we care
    about), plus ``_add_unable_to_address_issue_note`` so the error-recovery path doesn't try
    to render a template or hit the stub client. Returns the kwargs that ``create_daiv_agent``
    was called with.
    """
    captured: dict = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        raise _CapturedError

    with (
        patch("codebase.managers.issue_addressor.open_checkpointer", _noop_checkpointer),
        patch("codebase.managers.issue_addressor.create_daiv_agent", side_effect=_capture),
        patch.object(IssueAddressorManager, "_add_unable_to_address_issue_note"),
        pytest.raises(_CapturedError),
    ):
        await IssueAddressorManager.address_issue(issue=_issue(labels=labels), runtime_ctx=_ctx())

    return captured


class TestMaxLabelRoutesToMaxModel:
    """Lock the webhook → ``use_max`` → ``site_settings.agent_max_*`` contract."""

    async def test_max_label_resolves_to_max_model(self, stub_base_init):
        """``daiv-max`` label → primary model is ``site_settings.agent_max_model_name`` and
        thinking level is ``site_settings.agent_max_thinking_level``. The repo-config model
        is preserved as a fallback so the run degrades cleanly on provider outage."""
        captured = await _run_addressor(labels=["daiv-max"])

        model_names = captured["model_names"]
        assert model_names[0] == site_settings.agent_max_model_name
        assert captured["thinking_level"] == site_settings.agent_max_thinking_level
        # The repo-configured model survives as the next fallback in the chain — required
        # so a flaky max-model provider doesn't take the run down with it.
        assert RepositoryConfig().models.agent.model in model_names[1:]

    async def test_max_label_case_insensitive(self, stub_base_init):
        """Label matching must be case-insensitive — GitHub UIs upper-case labels freely."""
        captured = await _run_addressor(labels=["DAIV-MAX"])
        assert captured["model_names"][0] == site_settings.agent_max_model_name
        assert captured["thinking_level"] == site_settings.agent_max_thinking_level

    async def test_no_max_label_uses_repo_config_model(self, stub_base_init):
        """Without ``daiv-max`` the resolved primary model must come from the repo's
        ``AgentModelConfig`` — proving the ``use_max`` branch is the *only* path to the
        max model and isn't accidentally engaged on every webhook."""
        captured = await _run_addressor(labels=["daiv"])
        repo_agent_cfg = RepositoryConfig().models.agent

        assert captured["model_names"][0] == repo_agent_cfg.model
        assert captured["thinking_level"] == repo_agent_cfg.thinking_level
        # The max model must NOT leak into a non-max run.
        assert site_settings.agent_max_model_name not in captured["model_names"]
