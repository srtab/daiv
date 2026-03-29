from django.urls import path

from allauth.account import views as account_views
from allauth.socialaccount import views as socialaccount_views
from allauth.urls import build_provider_urlpatterns

from accounts.socialaccount import oauth2_callback, oauth2_login

urlpatterns = [
    # Custom GitLab OAuth adapter must come before auto-discovered provider
    # URLs so it takes precedence.  This lets us use a Docker-internal URL for
    # server-side token exchange while keeping the browser-facing authorize URL
    # unchanged.
    path("gitlab/login/", oauth2_login, name="gitlab_login"),
    path("gitlab/login/callback/", oauth2_callback, name="gitlab_callback"),
    # Account views (login, logout, login-by-code only — no signup, password,
    # or email management routes).
    path("login/", account_views.login, name="account_login"),
    path("logout/", account_views.logout, name="account_logout"),
    path("login/code/", account_views.request_login_code, name="account_request_login_code"),
    path("login/code/confirm/", account_views.confirm_login_code, name="account_confirm_login_code"),
    # Social account views required by the OAuth flow.
    path("3rdparty/login/cancelled/", socialaccount_views.login_cancelled, name="socialaccount_login_cancelled"),
    path("3rdparty/login/error/", socialaccount_views.login_error, name="socialaccount_login_error"),
    path("3rdparty/signup/", socialaccount_views.signup, name="socialaccount_signup"),
    # Auto-discovered OAuth provider URLs (GitHub, GitLab, etc.).
    *build_provider_urlpatterns(),
]
