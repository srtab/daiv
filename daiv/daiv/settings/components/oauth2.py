# ---------------------------------------------------------------------------
# django-oauth-toolkit
# ---------------------------------------------------------------------------

OAUTH2_PROVIDER = {
    "PKCE_REQUIRED": True,
    "ALLOWED_REDIRECT_URI_SCHEMES": ["http", "https"],
    "SCOPES": {"mcp": "Access MCP tools"},
    "DEFAULT_SCOPES": ["mcp"],
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 86400,
    "ROTATE_REFRESH_TOKEN": True,
    "OAUTH2_BACKEND_CLASS": "oauth2_provider.oauth2_backends.OAuthLibCore",
    "OIDC_ENABLED": False,
}
