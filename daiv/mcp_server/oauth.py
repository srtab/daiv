import json
import logging
import secrets
from http import HTTPStatus

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from oauth2_provider.models import Application

logger = logging.getLogger("daiv.mcp_server")


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
        "authorization_endpoint": request.build_absolute_uri("/oauth/authorize/"),
        "token_endpoint": request.build_absolute_uri("/oauth/token/"),
        "registration_endpoint": request.build_absolute_uri("/oauth/register/"),
        "revocation_endpoint": request.build_absolute_uri("/oauth/revoke_token/"),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["mcp"],
    })


@csrf_exempt
@require_POST
def oauth_register_client(request: HttpRequest) -> JsonResponse:
    """
    OAuth 2.0 Dynamic Client Registration (RFC 7591).

    MCP clients call this to register themselves as OAuth applications before
    starting the authorization flow. No authentication is required.
    """
    try:
        body = json.loads(request.body)
    except ValueError:
        return JsonResponse(
            {"error": "invalid_client_metadata", "error_description": "Request body must be valid JSON."},
            status=HTTPStatus.BAD_REQUEST,
        )

    client_name = body.get("client_name", "MCP Client")
    redirect_uris = body.get("redirect_uris", [])

    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JsonResponse(
            {"error": "invalid_client_metadata", "error_description": "redirect_uris must be a non-empty array."},
            status=HTTPStatus.BAD_REQUEST,
        )

    token_endpoint_auth_method = body.get("token_endpoint_auth_method", "none")
    if token_endpoint_auth_method not in ("none", "client_secret_post"):
        return JsonResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "token_endpoint_auth_method must be 'none' or 'client_secret_post'.",
            },
            status=HTTPStatus.BAD_REQUEST,
        )

    client_secret = ""
    client_type = Application.CLIENT_PUBLIC
    if token_endpoint_auth_method == "client_secret_post":  # noqa: S105
        client_secret = secrets.token_urlsafe(48)
        client_type = Application.CLIENT_CONFIDENTIAL

    application = Application.objects.create(
        name=client_name,
        client_type=client_type,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=" ".join(redirect_uris),
        client_secret=client_secret,
        skip_authorization=False,
    )

    logger.info("Registered MCP OAuth client: %s (client_id=%s)", client_name, application.client_id)

    response_data: dict[str, str | list[str]] = {
        "client_id": application.client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": token_endpoint_auth_method,
    }

    if client_secret:
        response_data["client_secret"] = client_secret

    return JsonResponse(response_data, status=HTTPStatus.CREATED)
