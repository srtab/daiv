from allauth.core import context
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.providers.gitlab.views import GitLabOAuth2Adapter
from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView, OAuth2LoginView


class GitLabServerAwareAdapter(GitLabOAuth2Adapter):
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


oauth2_login = OAuth2LoginView.adapter_view(GitLabServerAwareAdapter)
oauth2_callback = OAuth2CallbackView.adapter_view(GitLabServerAwareAdapter)
