import json

from django.test import RequestFactory

import pytest
from mcp_server.oauth import oauth_metadata


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
    assert not data["issuer"].endswith("/")


def test_oauth_metadata_contains_correct_endpoints(rf):
    request = rf.get("/.well-known/oauth-authorization-server")

    response = oauth_metadata(request)
    data = json.loads(response.content)

    assert data["authorization_endpoint"].endswith("/oauth/authorize/")
    assert data["token_endpoint"].endswith("/oauth/token/")
    assert data["registration_endpoint"].endswith("/api/oauth/register")
    assert data["revocation_endpoint"].endswith("/oauth/revoke_token/")
