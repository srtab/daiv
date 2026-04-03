from django.http import HttpRequest, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET


@require_GET
def oauth_metadata(request: HttpRequest) -> JsonResponse:
    """
    OAuth 2.0 Authorization Server Metadata (RFC 8414).

    Returns the metadata document so MCP clients (e.g. Claude Code) can discover
    the authorization, token, registration, and revocation endpoints.
    """
    base_url = request.build_absolute_uri("/")
    return JsonResponse({
        "issuer": base_url.rstrip("/"),
        "authorization_endpoint": request.build_absolute_uri(reverse("oauth2_provider:authorize")),
        "token_endpoint": request.build_absolute_uri(reverse("oauth2_provider:token")),
        "registration_endpoint": request.build_absolute_uri(reverse("api:oauth_register")),
        "revocation_endpoint": request.build_absolute_uri(reverse("oauth2_provider:revoke-token")),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["mcp"],
    })
