"""Tests for AgentRunCreateView (the "Start a run" form at runs:agent_run_new).

Replaces the deleted activity/test_views_runs.py suite. Covers: login gating, the
blank form, and the POST redirect branching (single run vs. batch/failure)
including the submit-failure re-render.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

from django.urls import reverse

import pytest
from sessions.models import Run
from sessions.services import BatchSubmitFailure, BatchSubmitResult

NEW_RUN_URL = reverse("runs:agent_run_new")


def _post_data(repo_id="acme/repo", ref=""):
    return {"prompt": "go", "repos": json.dumps([{"repo_id": repo_id, "ref": ref}])}


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
    assert "sandbox_envs" in resp.context
    assert not resp.context["form"].initial.get("prompt")


# --- POST ------------------------------------------------------------------


@pytest.mark.django_db
def test_post_single_run_redirects_to_batch_list(member_client):
    result = _fake_result(runs=1, failed=0, session_id="thread-abc")
    with (
        patch("sessions.views.resolve_repo_envs", side_effect=lambda *, user, repos, explicit_env_id: repos),
        patch("sessions.views.submit_batch_runs", return_value=result) as submit,
    ):
        resp = member_client.post(NEW_RUN_URL, _post_data())
    assert resp.status_code == 302
    # A single run now lands on the batch-scoped list (not the detail page), to
    # reinforce the "hand off and walk away" model.
    assert resp["Location"] == reverse("session_list") + f"?batch={result.batch_id}"
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


@pytest.mark.django_db
def test_post_access_revoked_before_submit_errors_on_repos(member_client):
    from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE, RepositoryAccessDenied

    with (
        patch("sessions.views.resolve_repo_envs", side_effect=lambda *, user, repos, explicit_env_id: repos),
        patch("sessions.admission.assert_can_run", side_effect=[None, RepositoryAccessDenied(["acme/repo"])]),
    ):
        resp = member_client.post(NEW_RUN_URL, _post_data())

    assert resp.status_code == 200
    assert resp.context["form"].errors["repos"] == [REPO_ACCESS_DENIED_MESSAGE]
    assert not Run.objects.exists()


# --- MCP overrides ---------------------------------------------------------


@pytest.mark.django_db
def test_run_form_submit_passes_mcp_overrides(member_client, member_user):
    import json

    from mcp_connectors.models import MCPServer
    from mcp_connectors.selection import build_selection_pool, effective_selection

    MCPServer.objects.create(
        name="b",
        scope=MCPServer.Visibility.GLOBAL,
        status=MCPServer.Status.ON_DEMAND,
        transport=MCPServer.Transport.HTTP,
        url="http://b.test/mcp",
    )
    pool = build_selection_pool(member_user.pk)
    # Default selection (no overrides) plus "b" opted in.
    selected = sorted(effective_selection({}, pool)) + ["b"]
    result = _fake_result(runs=1, failed=0, session_id="thread-abc")
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return result

    with (
        patch("sessions.views.resolve_repo_envs", side_effect=lambda *, user, repos, explicit_env_id: repos),
        patch("sessions.views.submit_batch_runs", side_effect=_capture),
    ):
        resp = member_client.post(NEW_RUN_URL, {**_post_data(), "mcp_servers": json.dumps(selected)})
    assert resp.status_code == 302
    assert captured.get("mcp_overrides") == {"b": "on"}
