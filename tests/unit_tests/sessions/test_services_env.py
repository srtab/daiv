"""Per-target sandbox-environment propagation in batch submit.

Restores coverage lost with the deleted activity/test_submit_batch_env.py: a
``RepoTarget.sandbox_environment_id`` must reach both the enqueued ``run_job_task``
and the created ``Run`` row, so a run executes in the intended sandbox.
"""

from __future__ import annotations

from unittest import mock

import pytest
from sandbox_envs.models import SandboxEnvironment
from sessions.models import SessionOrigin
from sessions.services import RepoTarget, submit_batch_runs

pytestmark = pytest.mark.django_db


def test_per_target_sandbox_env_reaches_task_and_run(member_user, create_db_task_result):
    env = SandboxEnvironment.objects.filter(scope="global").first()
    assert env is not None, "the global default sandbox env is seeded by migration"

    with mock.patch("sessions.services.run_job_task") as m_task:
        m_task.aenqueue = mock.AsyncMock(return_value=create_db_task_result())
        result = submit_batch_runs(
            user=member_user,
            prompt="do it",
            repos=[RepoTarget(repo_id="a/b", ref="", sandbox_environment_id=str(env.id))],
            notify_on=None,
            trigger_type=SessionOrigin.UI_JOB,
        )

    assert len(result.runs) == 1
    assert str(result.runs[0].sandbox_environment_id) == str(env.id)
    enqueue_kwargs = m_task.aenqueue.await_args.kwargs
    assert enqueue_kwargs["sandbox_environment_id"] == str(env.id)
