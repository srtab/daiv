from unittest.mock import patch

from django.urls import reverse

import pytest
from sandbox_envs.models import SandboxEnvironment, Visibility

URL_NAME = "api:chat_auto_environment"


@pytest.fixture(autouse=True)
def _no_seeded_global_envs(db):
    SandboxEnvironment.objects.filter(scope=Visibility.GLOBAL).delete()


def test_resolves_auto_for_a_readable_repository(member_client, member_user):
    SandboxEnvironment.objects.create(
        scope=Visibility.USER, user=member_user, name="mine", base_image="python:3.14", repo_ids=["acme/foo"]
    )

    response = member_client.get(reverse(URL_NAME), {"repo": "acme/foo"})

    assert response.status_code == 200
    assert response.json()["name"] == "mine"
    assert response.json()["scope"] == Visibility.USER


def test_resolves_to_nothing_without_a_match_or_default(member_client):
    response = member_client.get(reverse(URL_NAME), {"repo": "acme/foo"})

    assert response.status_code == 200
    assert response.json() == {}


def test_an_unreadable_repository_is_a_404(member_client, member_user):
    SandboxEnvironment.objects.create(
        scope=Visibility.USER, user=member_user, name="mine", base_image="python:3.14", repo_ids=["acme/foo"]
    )

    with patch("chat.api.views.can_view", return_value=False):
        response = member_client.get(reverse(URL_NAME), {"repo": "acme/foo"})

    assert response.status_code == 404
    assert "mine" not in response.content.decode()


def test_requires_authentication(client):
    response = client.get(reverse(URL_NAME), {"repo": "acme/foo"})

    assert response.status_code == 401
