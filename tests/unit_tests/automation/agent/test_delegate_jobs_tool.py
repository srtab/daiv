import json
from unittest.mock import AsyncMock, patch

import pytest
from sessions.models import Session, SessionOrigin

from automation.agent.middlewares.delegate_jobs import delegate_jobs_tool

pytestmark = pytest.mark.django_db(transaction=True)

CONFIG = {"configurable": {"thread_id": "coord-thread"}}


async def _invoke(goal, targets):
    return json.loads(await delegate_jobs_tool.ainvoke({"goal": goal, "targets": targets}, config=CONFIG))


async def test_refuses_when_session_has_no_user(django_user_model):
    await Session.objects.acreate(thread_id="coord-thread", origin=SessionOrigin.MCP_JOB, repo_id="g/coord", user=None)
    out = await _invoke("goal", [{"repo_id": "g/a", "prompt": "p"}])
    assert "no user" in out["error"].lower()


async def test_refuses_at_depth_cap(django_user_model):
    user = await django_user_model.objects.acreate(username="u1")
    await Session.objects.acreate(
        thread_id="coord-thread", origin=SessionOrigin.DELEGATED_JOB, repo_id="g/coord", user=user, spawn_depth=2
    )
    out = await _invoke("goal", [{"repo_id": "g/a", "prompt": "p"}])
    assert "depth" in out["error"].lower()


async def test_denied_targets_reported_inline(django_user_model):
    user = await django_user_model.objects.acreate(username="u2")
    await Session.objects.acreate(thread_id="coord-thread", origin=SessionOrigin.MCP_JOB, repo_id="g/coord", user=user)

    from codebase.authorization import RepositoryAccessDenied

    with patch(
        "automation.agent.middlewares.delegate_jobs.aassert_can_run",
        new=AsyncMock(side_effect=RepositoryAccessDenied(["g/a", "g/b"])),
    ):
        out = await _invoke("goal", [{"repo_id": "g/a", "prompt": "p"}, {"repo_id": "g/b", "prompt": "q"}])
    assert out["batch_id"] is None
    assert {f["repo_id"] for f in out["failed"]} == {"g/a", "g/b"}


async def test_success_path_submits_allowed_targets(django_user_model):
    user = await django_user_model.objects.acreate(username="u3")
    await Session.objects.acreate(thread_id="coord-thread", origin=SessionOrigin.MCP_JOB, repo_id="g/coord", user=user)

    fake_run = type("R", (), {"repo_id": "g/a", "ref": "", "session_id": "leg-1"})()
    fake_result = type("B", (), {"batch_id": "batch-1", "runs": [fake_run], "failed": []})()

    with (
        patch("automation.agent.middlewares.delegate_jobs.aassert_can_run", new=AsyncMock(return_value=None)),
        patch(
            "automation.agent.middlewares.delegate_jobs.aresolve_repo_envs",
            new=AsyncMock(side_effect=lambda **kw: kw["repos"]),
        ),
        patch(
            "automation.agent.middlewares.delegate_jobs.asubmit_batch_runs", new=AsyncMock(return_value=fake_result)
        ) as m_submit,
    ):
        out = await _invoke("goal", [{"repo_id": "g/a", "prompt": "do X"}])

    assert out["batch_id"] == "batch-1"
    assert out["delegated"][0]["repo_id"] == "g/a"
    assert out["delegated"][0]["session_url"] == "/dashboard/sessions/leg-1/"
    # parent_thread_id + spawn_depth were passed through
    kwargs = m_submit.call_args.kwargs
    assert kwargs["parent_thread_id"] == "coord-thread"
    assert kwargs["spawn_depth"] == 1
    assert kwargs["trigger_type"] == SessionOrigin.DELEGATED_JOB


def test_middleware_exposes_the_tool():
    from automation.agent.middlewares.delegate_jobs import DELEGATE_JOBS_NAME, DelegateJobsMiddleware

    mw = DelegateJobsMiddleware()
    assert [t.name for t in mw.tools] == [DELEGATE_JOBS_NAME]
