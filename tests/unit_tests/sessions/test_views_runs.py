"""Tests for AgentRunCreateView (the "Start a run" / retry form at runs:agent_run_new).

Replaces the deleted activity/test_views_runs.py suite. Covers: login gating, the
blank form, retry prefill from ``?from=<pk>``, the ownership + invalid-id guards on
that source lookup, and the POST redirect branching (single run vs. batch/failure)
including the submit-failure re-render.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

from django.urls import reverse

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.services import BatchSubmitFailure, BatchSubmitResult

NEW_RUN_URL = reverse("runs:agent_run_new")


def _create_run(user, **kwargs) -> Run:
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="acme/repo", user=user
    )
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.UI_JOB,
        "repo_id": "acme/repo",
        "ref": "main",
        "prompt": "original prompt",
        "agent_model": "anthropic:claude",
        "status": RunStatus.SUCCESSFUL,
        "user": user,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


def _post_data(repo_id="acme/repo", ref=""):
    return {"prompt": "go", "repos": json.dumps([{"repo_id": repo_id, "ref": ref}]), "notify_on": "never"}


def _fake_result(*, runs=1, failed=0, session_id="sess-123"):
    result = MagicMock(spec=BatchSubmitResult)
    result.batch_id = uuid.uuid4()
    result.runs = [MagicMock(session_id=session_id) for _ in range(runs)]
    result.failed = [BatchSubmitFailure(repo_id="acme/repo", ref="", error="boom") for _ in range(failed)]
    return result


# --- GET -------------------------------------------------------------------


@pytest.mark.django_db
def test_get_requires_login(client):
    resp = client.get(NEW_RUN_URL)
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()


@pytest.mark.django_db
def test_get_blank_form_renders_with_pickers(member_client):
    resp = member_client.get(NEW_RUN_URL)
    assert resp.status_code == 200
    assert resp.context["source_run"] is None
    assert "sandbox_envs" in resp.context
    assert not resp.context["form"].initial.get("prompt")


@pytest.mark.django_db
def test_get_retry_prefills_from_owned_source_run(member_client, member_user):
    source = _create_run(member_user, prompt="retry me", agent_model="anthropic:opus")
    resp = member_client.get(NEW_RUN_URL, {"from": str(source.pk)})
    assert resp.status_code == 200
    assert resp.context["source_run"].pk == source.pk
    initial = resp.context["form"].initial
    assert initial["prompt"] == "retry me"
    assert initial["agent_model"] == "anthropic:opus"
    assert initial["repos"] == [{"repo_id": "acme/repo", "ref": "main"}]


@pytest.mark.django_db
def test_get_retry_from_other_users_run_is_not_prefilled(member_client, other_user):
    """by_owner scoping hides another user's run — the form falls back to blank (not a 404)."""
    foreign = _create_run(other_user)
    resp = member_client.get(NEW_RUN_URL, {"from": str(foreign.pk)})
    assert resp.status_code == 200
    assert resp.context["source_run"] is None
    assert not resp.context["form"].initial.get("prompt")


@pytest.mark.django_db
def test_get_retry_with_invalid_uuid_is_404(member_client):
    resp = member_client.get(NEW_RUN_URL, {"from": "not-a-uuid"})
    assert resp.status_code == 404


@pytest.mark.django_db
def test_get_retry_with_unknown_uuid_falls_back_to_blank(member_client):
    resp = member_client.get(NEW_RUN_URL, {"from": str(uuid.uuid4())})
    assert resp.status_code == 200
    assert resp.context["source_run"] is None


# --- POST ------------------------------------------------------------------


@pytest.mark.django_db
def test_post_single_run_redirects_to_session_detail(member_client):
    result = _fake_result(runs=1, failed=0, session_id="thread-abc")
    with (
        patch("sessions.views.resolve_repo_envs", side_effect=lambda *, user, repos, explicit_env_id: repos),
        patch("sessions.views.submit_batch_runs", return_value=result) as submit,
    ):
        resp = member_client.post(NEW_RUN_URL, _post_data())
    assert resp.status_code == 302
    assert resp["Location"] == reverse("session_detail", kwargs={"thread_id": "thread-abc"})
    submit.assert_called_once()


@pytest.mark.django_db
def test_post_multiple_runs_redirects_to_batch_list(member_client):
    result = _fake_result(runs=2, failed=0)
    with (
        patch("sessions.views.resolve_repo_envs", side_effect=lambda *, user, repos, explicit_env_id: repos),
        patch("sessions.views.submit_batch_runs", return_value=result),
    ):
        resp = member_client.post(NEW_RUN_URL, _post_data())
    assert resp.status_code == 302
    assert resp["Location"] == reverse("session_list") + f"?batch={result.batch_id}"


@pytest.mark.django_db
def test_post_with_failures_warns_and_redirects_to_batch(member_client):
    result = _fake_result(runs=1, failed=1)
    with (
        patch("sessions.views.resolve_repo_envs", side_effect=lambda *, user, repos, explicit_env_id: repos),
        patch("sessions.views.submit_batch_runs", return_value=result),
    ):
        resp = member_client.post(NEW_RUN_URL, _post_data(), follow=False)
    # A partial failure never collapses to the single-run detail redirect.
    assert resp.status_code == 302
    assert f"?batch={result.batch_id}" in resp["Location"]


@pytest.mark.django_db
def test_post_submit_failure_rerenders_form_with_error(member_client):
    with (
        patch("sessions.views.resolve_repo_envs", side_effect=lambda *, user, repos, explicit_env_id: repos),
        patch("sessions.views.submit_batch_runs", side_effect=RuntimeError("broker down")),
    ):
        resp = member_client.post(NEW_RUN_URL, _post_data())
    assert resp.status_code == 200
    # Non-field error surfaced and the page re-rendered rather than redirecting.
    assert resp.context["form"].errors
