"""Authorization enforcement at the batch-submission chokepoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sessions.models import Run, SessionOrigin
from sessions.services import RepoTarget, asubmit_batch_runs

from codebase.authorization import RepositoryAccessDenied


@pytest.mark.django_db(transaction=True)
async def test_denied_user_gets_no_runs(member_user):
    with (
        patch("sessions.services.aassert_can_run", new=AsyncMock(side_effect=RepositoryAccessDenied(["a/b"]))),
        pytest.raises(RepositoryAccessDenied),
    ):
        await asubmit_batch_runs(
            user=member_user,
            prompt="p",
            repos=[RepoTarget(repo_id="a/b", ref="")],
            notify_on=None,
            trigger_type=SessionOrigin.API_JOB,
        )
    assert await Run.objects.acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_webhook_style_submission_skips_check():
    check = AsyncMock(return_value=None)
    with patch("sessions.services.aassert_can_run", new=check), patch("sessions.services.run_job_task") as m_task:
        m_task.aenqueue = AsyncMock(side_effect=Exception("stop before enqueue matters"))
        await asubmit_batch_runs(
            user=None,
            prompt="p",
            repos=[RepoTarget(repo_id="a/b", ref="")],
            notify_on=None,
            trigger_type=SessionOrigin.SCHEDULE,
            external_username="webhooker",
        )
    check.assert_not_awaited()
