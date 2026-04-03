import json

from django.test import RequestFactory

import pytest
from mcp_server.oauth import oauth_metadata, oauth_register_client


@pytest.fixture
def rf():
    return RequestFactory()


def test_oauth_metadata_returns_expected_fields(rf):
    request = rf.get("/.well-known/oauth-authorization-server")
    request.META["SERVER_NAME"] = "testserver"
    request.META["SERVER_PORT"] = "443"

    response = oauth_metadata(request)

    assert response.status_code == 200
    data = json.loads(response.content)
    assert "authorization_endpoint" in data
    assert "token_endpoint" in data
    assert "registration_endpoint" in data
    assert "revocation_endpoint" in data
    assert data["response_types_supported"] == ["code"]
    assert data["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert data["code_challenge_methods_supported"] == ["S256"]
    assert data["scopes_supported"] == ["mcp"]
    assert "issuer" in data


def test_oauth_metadata_contains_correct_endpoints(rf):
    request = rf.get("/.well-known/oauth-authorization-server")

    response = oauth_metadata(request)
    data = json.loads(response.content)

    assert "/oauth/authorize/" in data["authorization_endpoint"]
    assert "/oauth/token/" in data["token_endpoint"]
    assert "/oauth/register/" in data["registration_endpoint"]
    assert "/oauth/revoke_token/" in data["revocation_endpoint"]


@pytest.mark.django_db
def test_register_client_success(rf):
    request = rf.post(
        "/oauth/register/",
        data=json.dumps({"client_name": "Test MCP Client", "redirect_uris": ["http://localhost:8080/callback"]}),
        content_type="application/json",
    )

    response = oauth_register_client(request)

    assert response.status_code == 201
    data = json.loads(response.content)
    assert "client_id" in data
    assert data["client_name"] == "Test MCP Client"
    assert data["redirect_uris"] == ["http://localhost:8080/callback"]
    assert data["grant_types"] == ["authorization_code", "refresh_token"]
    assert data["response_types"] == ["code"]
    assert data["token_endpoint_auth_method"] == "none"  # noqa: S105
    assert "client_secret" not in data


@pytest.mark.django_db
def test_register_client_confidential(rf):
    request = rf.post(
        "/oauth/register/",
        data=json.dumps({
            "client_name": "Confidential Client",
            "redirect_uris": ["http://localhost:8080/callback"],
            "token_endpoint_auth_method": "client_secret_post",
        }),
        content_type="application/json",
    )

    response = oauth_register_client(request)

    assert response.status_code == 201
    data = json.loads(response.content)
    assert "client_secret" in data
    assert len(data["client_secret"]) > 0


def test_register_client_invalid_json(rf):
    request = rf.post("/oauth/register/", data="not json", content_type="application/json")

    response = oauth_register_client(request)

    assert response.status_code == 400
    data = json.loads(response.content)
    assert data["error"] == "invalid_client_metadata"


def test_register_client_missing_redirect_uris(rf):
    request = rf.post("/oauth/register/", data=json.dumps({"client_name": "Test"}), content_type="application/json")

    response = oauth_register_client(request)

    assert response.status_code == 400
    data = json.loads(response.content)
    assert data["error"] == "invalid_client_metadata"


def test_register_client_empty_redirect_uris(rf):
    request = rf.post(
        "/oauth/register/",
        data=json.dumps({"client_name": "Test", "redirect_uris": []}),
        content_type="application/json",
    )

    response = oauth_register_client(request)

    assert response.status_code == 400


def test_register_client_invalid_auth_method(rf):
    request = rf.post(
        "/oauth/register/",
        data=json.dumps({
            "client_name": "Test",
            "redirect_uris": ["http://localhost/callback"],
            "token_endpoint_auth_method": "client_secret_basic",
        }),
        content_type="application/json",
    )

    response = oauth_register_client(request)

    assert response.status_code == 400
    data = json.loads(response.content)
    assert data["error"] == "invalid_client_metadata"
