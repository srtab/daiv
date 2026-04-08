import logging
import secrets

from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from ninja import Router
from ninja.throttling import AnonRateThrottle
from oauth2_provider.models import Application

from .schemas import ClientRegistrationRequest, ClientRegistrationResponse

logger = logging.getLogger("daiv.mcp_server")

oauth_router = Router(tags=["oauth"])


@oauth_router.post(
    "/register",
    response={201: ClientRegistrationResponse},
    auth=None,
    throttle=[AnonRateThrottle("5/m")],
    url_name="oauth_register",
)
def register_client(request: HttpRequest, payload: ClientRegistrationRequest) -> tuple[int, ClientRegistrationResponse]:
    """
    OAuth 2.0 Dynamic Client Registration (RFC 7591).

    MCP clients call this to register themselves as OAuth applications before
    starting the authorization flow. No authentication is required, as per
    RFC 7591 open registration -- MCP clients need this endpoint to obtain
    credentials before they can authenticate.
    """
    client_secret = ""
    client_type = Application.CLIENT_PUBLIC
    if payload.token_endpoint_auth_method == "client_secret_post":  # noqa: S105
        client_secret = secrets.token_urlsafe(48)
        client_type = Application.CLIENT_CONFIDENTIAL

    try:
        application = Application.objects.create(
            name=payload.client_name,
            client_type=client_type,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris=" ".join(payload.redirect_uris),
            client_secret=client_secret,
            skip_authorization=False,
        )
    except Exception:
        logger.exception("Failed to create OAuth application for client: %s", payload.client_name)
        raise

    logger.info("Registered MCP OAuth client: %s (client_id=%s)", payload.client_name, application.client_id)

    return 201, ClientRegistrationResponse(
        client_id=application.client_id,
        client_name=payload.client_name,
        redirect_uris=payload.redirect_uris,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method=payload.token_endpoint_auth_method,
        client_secret=client_secret or None,
    )
