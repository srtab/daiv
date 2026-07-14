import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sessions.models import Session, SessionOrigin

from automation.agent.middlewares.delegate_jobs import delegate_jobs_tool

pytestmark = pytest.mark.django_db(transaction=True)

CONFIG = {"configurable": {"thread_id": "coord-thread"}}


async def _invoke(goal, targets):
    return json.loads(await delegate_jobs_tool.ainvoke({"goal": goal, "targets": targets}, config=CONFIG))


def _fake_result(repo_id="g/a", session_id="leg-1", batch_id="batch-1"):
    run = type("R", (), {"repo_id": repo_id, "ref": "", "session_id": session_id})()
    return type("B", (), {"batch_id": batch_id, "runs": [run], "failed": []})()


@contextmanager
def _patched_delegate(submit):
    """Patch the tool's auth + env-resolution + batch-submit collaborators.

    ``submit`` is the AsyncMock used for ``asubmit_batch_runs`` (a return_value for the happy path or
    a side_effect to simulate a raise); it is yielded so callers can assert on its call args.
    """
    with (
        patch("automation.agent.middlewares.delegate_jobs.aassert_can_run", new=AsyncMock(return_value=None)),
        patch(
            "automation.agent.middlewares.delegate_jobs.aresolve_repo_envs",
            new=AsyncMock(side_effect=lambda **kw: kw["repos"]),
        ),
        patch("automation.agent.middlewares.delegate_jobs.asubmit_batch_runs", new=submit),
    ):
        yield submit


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

    with _patched_delegate(AsyncMock(return_value=_fake_result())) as m_submit:
        out = await _invoke("goal", [{"repo_id": "g/a", "prompt": "do X"}])

    assert out["batch_id"] == "batch-1"
    assert out["delegated"][0]["repo_id"] == "g/a"
    assert out["delegated"][0]["session_url"] == "/dashboard/sessions/leg-1/"
    # parent_thread_id + spawn_depth were passed through
    kwargs = m_submit.call_args.kwargs
    assert kwargs["parent_thread_id"] == "coord-thread"
    assert kwargs["spawn_depth"] == 1
    assert kwargs["trigger_type"] == SessionOrigin.DELEGATED_JOB


async def test_refuses_empty_targets(django_user_model):
    user = await django_user_model.objects.acreate(username="u-empty")
    await Session.objects.acreate(thread_id="coord-thread", origin=SessionOrigin.MCP_JOB, repo_id="g/coord", user=user)
    out = await _invoke("goal", [])
    assert "at least one target" in out["error"].lower()


async def test_refuses_more_than_max_targets(django_user_model):
    from sessions.services import MAX_DELEGATED_TARGETS

    user = await django_user_model.objects.acreate(username="u-many")
    await Session.objects.acreate(thread_id="coord-thread", origin=SessionOrigin.MCP_JOB, repo_id="g/coord", user=user)
    targets = [{"repo_id": f"g/r{i}", "prompt": "p"} for i in range(MAX_DELEGATED_TARGETS + 1)]
    out = await _invoke("goal", targets)
    assert "at most" in out["error"].lower()


async def test_refuses_duplicate_target(django_user_model):
    user = await django_user_model.objects.acreate(username="u-dup")
    await Session.objects.acreate(thread_id="coord-thread", origin=SessionOrigin.MCP_JOB, repo_id="g/coord", user=user)
    # Same repo, both with the default (omitted) ref → collide on ("g/a", "").
    out = await _invoke("goal", [{"repo_id": "g/a", "prompt": "p"}, {"repo_id": "g/a", "prompt": "q"}])
    assert "duplicate target" in out["error"].lower()


async def test_refuses_self_delegation(django_user_model):
    """Delegating to the coordinator's own repo+ref is refused and steered to subagents (`task`)."""
    user = await django_user_model.objects.acreate(username="u-self")
    await Session.objects.acreate(
        thread_id="coord-thread", origin=SessionOrigin.MCP_JOB, repo_id="g/coord", ref="main", user=user
    )
    out = await _invoke("goal", [{"repo_id": "g/coord", "ref": "main", "prompt": "p"}])
    assert "g/coord" in out["error"]
    assert "task" in out["error"].lower()


async def test_self_delegation_fails_whole_call_without_submitting(django_user_model):
    """A self-target (same repo+ref) mixed with valid targets fails the entire call — nothing submitted."""
    user = await django_user_model.objects.acreate(username="u-self-mix")
    await Session.objects.acreate(
        thread_id="coord-thread", origin=SessionOrigin.MCP_JOB, repo_id="g/coord", ref="main", user=user
    )
    with _patched_delegate(AsyncMock(return_value=_fake_result())) as m_submit:
        out = await _invoke(
            "goal", [{"repo_id": "g/other", "prompt": "p"}, {"repo_id": "g/coord", "ref": "main", "prompt": "q"}]
        )
    assert "task" in out["error"].lower()
    m_submit.assert_not_called()


async def test_allows_same_repo_different_ref(django_user_model):
    """Same repo on a *different* ref is a distinct checkout, not self-delegation — it delegates."""
    user = await django_user_model.objects.acreate(username="u-diff-ref")
    await Session.objects.acreate(
        thread_id="coord-thread", origin=SessionOrigin.MCP_JOB, repo_id="g/coord", ref="main", user=user
    )
    with _patched_delegate(AsyncMock(return_value=_fake_result())) as m_submit:
        out = await _invoke("goal", [{"repo_id": "g/coord", "ref": "feature-x", "prompt": "p"}])
    assert out["batch_id"] == "batch-1"
    m_submit.assert_called_once()


async def test_allows_just_under_depth_cap(django_user_model):
    """A coordinator at spawn_depth=1 (one below the cap) delegates, stamping legs at depth 2."""
    user = await django_user_model.objects.acreate(username="u-boundary")
    await Session.objects.acreate(
        thread_id="coord-thread", origin=SessionOrigin.DELEGATED_JOB, repo_id="g/coord", user=user, spawn_depth=1
    )

    with _patched_delegate(AsyncMock(return_value=_fake_result())) as m_submit:
        out = await _invoke("goal", [{"repo_id": "g/a", "prompt": "do X"}])

    assert out["batch_id"] == "batch-1"
    assert m_submit.call_args.kwargs["spawn_depth"] == 2


async def test_submission_failure_returns_json_error(django_user_model):
    """A raise from env resolution / batch submit is reported as a JSON error, not propagated."""
    user = await django_user_model.objects.acreate(username="u-boom")
    await Session.objects.acreate(thread_id="coord-thread", origin=SessionOrigin.MCP_JOB, repo_id="g/coord", user=user)

    with _patched_delegate(AsyncMock(side_effect=RuntimeError("db exploded"))):
        out = await _invoke("goal", [{"repo_id": "g/a", "prompt": "do X"}])

    assert "error" in out
    assert "submission failed" in out["error"].lower()


def test_middleware_exposes_the_tool():
    from automation.agent.middlewares.delegate_jobs import DELEGATE_JOBS_NAME, DelegateJobsMiddleware

    mw = DelegateJobsMiddleware()
    assert [t.name for t in mw.tools] == [DELEGATE_JOBS_NAME]
