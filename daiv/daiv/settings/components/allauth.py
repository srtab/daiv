# ---------------------------------------------------------------------------
# django-allauth
# ---------------------------------------------------------------------------

from decouple import Csv, config

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*"]
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

# Narrow to `read_user read_api` for read-only cross-project context. GitHub's list is inert: a
# GitHub App ignores the OAuth `scope` parameter and derives reach from its installed permissions.
GITLAB_OAUTH_SCOPE = config("DAIV_GITLAB_OAUTH_SCOPE", default="read_user api", cast=Csv(delimiter=" "))

# Provider scopes are always registered; whether a provider is actually usable
# (credentials, URLs) is determined at runtime by SocialAccountAdapter.list_apps().
SOCIALACCOUNT_PROVIDERS = {"github": {"SCOPE": ["user:email"]}, "gitlab": {"SCOPE": GITLAB_OAUTH_SCOPE}}
