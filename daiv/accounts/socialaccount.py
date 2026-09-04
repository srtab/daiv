from allauth.core import context
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.providers.github.views import GitHubOAuth2Adapter
from allauth.socialaccount.providers.gitlab.views import GitLabOAuth2Adapter
from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView, OAuth2LoginView

from codebase.conf import settings as codebase_settings

TOKEN_RESPONSE_ATTR = "_daiv_token_response"  # noqa: S105


class TokenResponseCaptureMixin:
    """Keep the raw token response reachable from the parsed ``SocialToken``.

    allauth's ``parse_token`` keeps only the access token, the refresh token and the expiry. The
    granted ``scope`` exists nowhere else, and DAIV records what the platform actually granted
    rather than what was requested — see ``accounts.adapter.SocialAccountAdapter``.
    """

    def parse_token(self, data):
        token = super().parse_token(data)
        setattr(token, TOKEN_RESPONSE_ATTR, dict(data))
        return token


class GitLabServerAwareAdapter(TokenResponseCaptureMixin, GitLabOAuth2Adapter):
    """
    GitLab adapter that supports a separate ``gitlab_server_url`` app setting
    for server-side HTTP calls (token exchange, profile fetch).  This is needed
    in Docker/compose environments where the browser-facing URL differs from
    the URL reachable inside the container network.

    When ``gitlab_server_url`` is empty or absent the adapter falls back to the
    standard ``gitlab_url``.
    """

    def _build_server_url(self, path):
        app = get_adapter().get_app(context.request, provider=self.provider_id)
        server_url = app.settings.get("gitlab_server_url")
        if server_url:
            return f"{server_url}{path}"
        return self._build_url(path)

    @property
    def access_token_url(self):
        return self._build_server_url("/oauth/token")

    @property
    def profile_url(self):
        return self._build_server_url(f"/api/{self.provider_api_version}/user")


class GitHubAppOAuth2Adapter(TokenResponseCaptureMixin, GitHubOAuth2Adapter):
    """GitHub App user-to-server flow, reusing the App this deployment already runs on.

    The endpoints are the OAuth App endpoints, so only what differs lives here:

    * the host comes from ``CODEBASE_GITHUB_URL`` (GitHub Enterprise) instead of a provider
      setting the stock adapter reads once at class-definition time;
    * a user-to-server token's reach is the intersection of the person's own permissions and the
      App's installed permissions — GitHub ignores the requested ``scope`` entirely;
    * the token expires only when the App has "Expire user authorization tokens" enabled, so both
      the expiring (refresh token issued) and non-expiring shapes must work. allauth's base
      ``parse_token`` already handles both.
    """

    @staticmethod
    def _web_url() -> str:
        url = codebase_settings.GITHUB_URL
        return str(url).rstrip("/") if url is not None else "https://github.com"

    @classmethod
    def _api_url(cls) -> str:
        web_url = cls._web_url()
        return "https://api.github.com" if web_url == "https://github.com" else f"{web_url}/api/v3"

    @property
    def access_token_url(self):
        return f"{self._web_url()}/login/oauth/access_token"

    @property
    def authorize_url(self):
        return f"{self._web_url()}/login/oauth/authorize"

    @property
    def profile_url(self):
        return f"{self._api_url()}/user"

    @property
    def emails_url(self):
        return f"{self._api_url()}/user/emails"


oauth2_login = OAuth2LoginView.adapter_view(GitLabServerAwareAdapter)
oauth2_callback = OAuth2CallbackView.adapter_view(GitLabServerAwareAdapter)

github_oauth2_login = OAuth2LoginView.adapter_view(GitHubAppOAuth2Adapter)
github_oauth2_callback = OAuth2CallbackView.adapter_view(GitHubAppOAuth2Adapter)
