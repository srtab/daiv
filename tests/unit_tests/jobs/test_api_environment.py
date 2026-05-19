import json
import uuid
from unittest.mock import AsyncMock, patch

from django.test import Client

import pytest
from asgiref.sync import async_to_sync
from sandbox_envs.models import SandboxEnvironment, Scope

from accounts.models import APIKey, User


@pytest.fixture
def auth_pair(db):
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    _, raw_key = async_to_sync(APIKey.objects.create_key)(user=user, name="t", expires_at=None)
    return user, raw_key


@pytest.mark.django_db
def test_jobs_endpoint_accepts_environment_name(auth_pair):
    user, key = auth_pair
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="alpine:latest")
    fake_activity = type("A", (), {"task_result_id": uuid.uuid4()})()
    with patch("jobs.api.views.asubmit_batch_runs", AsyncMock()) as submit:
        submit.return_value = type("R", (), {"batch_id": uuid.uuid4(), "activities": [fake_activity], "failed": []})()
        c = Client()
        resp = c.post(
            "/api/jobs",
            data=json.dumps({"prompt": "do work", "repos": [{"repo_id": "r/p", "ref": ""}], "environment": "dev"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {key}",
        )
    assert resp.status_code == 202
    targets = submit.await_args.kwargs["repos"]
    assert [t.sandbox_environment_id for t in targets] == [str(env.id)]


@pytest.mark.django_db
def test_jobs_endpoint_unknown_environment_400(auth_pair):
    user, key = auth_pair
    c = Client()
    resp = c.post(
        "/api/jobs",
        data=json.dumps({"prompt": "do work", "repos": [{"repo_id": "r/p", "ref": ""}], "environment": "nope"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {key}",
    )
    assert resp.status_code == 400
