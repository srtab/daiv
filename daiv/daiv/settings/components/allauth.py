# ---------------------------------------------------------------------------
# django-allauth
# ---------------------------------------------------------------------------

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

# Passkeys only: this feature is passwordless login, not general 2FA.
MFA_SUPPORTED_TYPES = ["webauthn"]
MFA_PASSKEY_LOGIN_ENABLED = True
# Passkey signup stays off: users are created by admins (AccountAdapter.is_open_for_signup).
# Global switch, not passkey-scoped: it also enables allauth's socialaccount
# connected/disconnected notifications, which templates/socialaccount/email/ styles.
ACCOUNT_EMAIL_NOTIFICATIONS = True

# Provider scopes are always registered; whether a provider is actually usable
# (credentials, URLs) is determined at runtime by SocialAccountAdapter.list_apps().
SOCIALACCOUNT_PROVIDERS = {"github": {"SCOPE": ["user:email"]}, "gitlab": {"SCOPE": ["read_user"]}}
