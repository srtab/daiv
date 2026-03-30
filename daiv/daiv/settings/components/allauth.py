import logging

from decouple import config
from get_docker_secret import get_docker_secret

logger = logging.getLogger("daiv.settings")

# ---------------------------------------------------------------------------
# django-allauth
# ---------------------------------------------------------------------------

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_EMAIL_REQUIRED = True
# Email verification is skipped because users authenticate via social providers
# (which verify emails) or via login-by-code (which proves email ownership).
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_LOGIN_BY_CODE_ENABLED = True
ACCOUNT_LOGIN_BY_CODE_MAX_ATTEMPTS = 3
ACCOUNT_LOGIN_BY_CODE_TIMEOUT = 300
ACCOUNT_ADAPTER = "accounts.adapter.AccountAdapter"
SOCIALACCOUNT_ADAPTER = "accounts.adapter.SocialAccountAdapter"
ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS = False

SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

LOGIN_REDIRECT_URL = "/dashboard/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/accounts/login/"
LOGIN_URL = "/accounts/login/"

# Register social providers only when credentials are fully configured.
# This prevents rendering login buttons that lead to broken OAuth flows.
SOCIALACCOUNT_PROVIDERS = {}


def _register_provider(name, scope, app_config):
    client_id = get_docker_secret(f"ALLAUTH_{name.upper()}_CLIENT_ID", default="")
    secret = get_docker_secret(f"ALLAUTH_{name.upper()}_SECRET", default="")
    if client_id and secret:
        app = {"client_id": client_id, "secret": secret, **app_config}
        SOCIALACCOUNT_PROVIDERS[name] = {"SCOPE": scope, "APPS": [app]}
    elif bool(client_id) != bool(secret):
        logger.warning(
            "Partial %s OAuth config: set both ALLAUTH_%s_CLIENT_ID and ALLAUTH_%s_SECRET, or neither.",
            name.capitalize(),
            name.upper(),
            name.upper(),
        )


_register_provider("github", scope=["user:email"], app_config={})
_register_provider(
    "gitlab",
    scope=["read_user"],
    app_config={
        "settings": {
            "gitlab_url": config("ALLAUTH_GITLAB_URL", default="https://gitlab.com"),
            "gitlab_server_url": config("ALLAUTH_GITLAB_SERVER_URL", default=""),
        }
    },
)
