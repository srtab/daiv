from accounts.socialaccount import GitHubAppOAuth2Adapter


def test_github_enterprise_never_posts_the_secret_to_github_com(settings, monkeypatch):
    monkeypatch.setattr("accounts.socialaccount.codebase_settings.GITHUB_URL", "https://ghe.example.com")
    assert GitHubAppOAuth2Adapter.access_token_endpoint() == "https://ghe.example.com/login/oauth/access_token"
