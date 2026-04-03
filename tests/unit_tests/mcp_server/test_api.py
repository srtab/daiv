import json

from django.core.cache import cache

import pytest


@pytest.fixture
def api_client():
    from django.test import Client

    return Client()


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    """Clear the cache before each test to avoid rate limiting across tests."""
    cache.clear()


@pytest.mark.django_db
def test_register_client_success(api_client):
    response = api_client.post(
        "/api/oauth/register",
        data=json.dumps({"client_name": "Test MCP Client", "redirect_uris": ["http://localhost:8080/callback"]}),
        content_type="application/json",
    )

    assert response.status_code == 201
    data = response.json()
    assert "client_id" in data
    assert data["client_name"] == "Test MCP Client"
    assert data["redirect_uris"] == ["http://localhost:8080/callback"]
    assert data["grant_types"] == ["authorization_code", "refresh_token"]
    assert data["response_types"] == ["code"]
    assert data["token_endpoint_auth_method"] == "none"  # noqa: S105
    assert data["client_secret"] is None


@pytest.mark.django_db
def test_register_client_confidential(api_client):
    response = api_client.post(
        "/api/oauth/register",
        data=json.dumps({
            "client_name": "Confidential Client",
            "redirect_uris": ["http://localhost:8080/callback"],
            "token_endpoint_auth_method": "client_secret_post",
        }),
        content_type="application/json",
    )

    assert response.status_code == 201
    data = response.json()
    assert data["client_secret"] is not None
    assert len(data["client_secret"]) > 0


@pytest.mark.django_db
def test_register_client_default_client_name(api_client):
    response = api_client.post(
        "/api/oauth/register",
        data=json.dumps({"redirect_uris": ["http://localhost/callback"]}),
        content_type="application/json",
    )

    assert response.status_code == 201
    data = response.json()
    assert data["client_name"] == "MCP Client"


def test_register_client_invalid_json(api_client):
    response = api_client.post("/api/oauth/register", data="not json", content_type="application/json")

    assert response.status_code == 400


def test_register_client_missing_redirect_uris(api_client):
    response = api_client.post(
        "/api/oauth/register", data=json.dumps({"client_name": "Test"}), content_type="application/json"
    )

    assert response.status_code == 422


def test_register_client_empty_redirect_uris(api_client):
    response = api_client.post(
        "/api/oauth/register",
        data=json.dumps({"client_name": "Test", "redirect_uris": []}),
        content_type="application/json",
    )

    assert response.status_code == 422


def test_register_client_invalid_auth_method(api_client):
    response = api_client.post(
        "/api/oauth/register",
        data=json.dumps({
            "client_name": "Test",
            "redirect_uris": ["http://localhost/callback"],
            "token_endpoint_auth_method": "client_secret_basic",
        }),
        content_type="application/json",
    )

    assert response.status_code == 422
