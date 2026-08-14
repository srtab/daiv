from unittest.mock import AsyncMock, patch

import pytest
from sessions.models import Session, SessionOrigin
from sessions.services import RepoTarget, asubmit_batch_runs

pytestmark = pytest.mark.django_db(transaction=True)


async def _run_batch(**kwargs):
    """Submit a batch with run_job_task.aenqueue stubbed so no broker is needed."""
    with patch("sessions.services.run_job_task") as m_task:
        m_task.aenqueue = AsyncMock(return_value=type("T", (), {"id": None})())
        return await asubmit_batch_runs(**kwargs)


async def test_per_target_prompt_overrides_batch_prompt():
    result = await _run_batch(
        user=None,
        prompt="batch-level goal",
        repos=[
            RepoTarget(repo_id="g/a", prompt="do X in A"),
            RepoTarget(repo_id="g/b"),  # no override → falls back to batch prompt
        ],
        trigger_type=SessionOrigin.DELEGATED_JOB,
    )
    by_repo = {r.repo_id: r for r in result.runs}
    assert by_repo["g/a"].prompt == "do X in A"
    assert by_repo["g/b"].prompt == "batch-level goal"


async def test_parentage_and_depth_stamped_on_leg_sessions():
    result = await _run_batch(
        user=None,
        prompt="goal",
        repos=[RepoTarget(repo_id="g/a", prompt="p")],
        trigger_type=SessionOrigin.DELEGATED_JOB,
        parent_thread_id="parent-thread-123",
        spawn_depth=2,
    )
    leg = result.runs[0]
    session = await Session.objects.aget(thread_id=leg.session_id)
    assert session.parent_thread_id == "parent-thread-123"
    assert session.spawn_depth == 2
