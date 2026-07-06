import base64

from pydantic import SecretStr

from codebase.clients.base import GitAuthEnv


def test_git_auth_env_as_env_builds_host_scoped_extraheader_config():
    """as_env() must authenticate git-over-HTTPS without the token ever reaching argv or .git/config:
    a command-scoped http.<origin>.extraheader plus fully disabled prompting (so a *rejected*
    credential fails fast with git's 'could not read Username' auth marker instead of hanging).
    Both GIT_TERMINAL_PROMPT=0 and GIT_ASKPASS='' are required: the empty GIT_ASKPASS short-circuits
    git's fallback to an inherited SSH_ASKPASS GUI helper, which TERMINAL_PROMPT alone does not."""
    env = GitAuthEnv.for_token("https://gitlab.com/group/repo.git", "tok-123").as_env()

    assert env == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://gitlab.com/.extraheader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic " + base64.b64encode(b"oauth2:tok-123").decode(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
    }


def test_git_auth_env_preserves_scheme_and_port():
    """git matches http.<url>.* config by URL prefix — dropping a non-default port (or the scheme)
    would silently never match, leaving every request unauthenticated."""
    env = GitAuthEnv.for_token("http://gitlab.local:8080/group/repo.git", "tok").as_env()

    assert env["GIT_CONFIG_KEY_0"] == "http.http://gitlab.local:8080/.extraheader"


def test_git_auth_env_holds_header_as_secret():
    """The credential must be wrapped in SecretStr so it never appears verbatim in a repr / log /
    Sentry stack-local — the same protection GitEgressCredential uses. Only as_env() (called at the
    subprocess/clone boundary) materialises the plaintext."""
    auth = GitAuthEnv.for_token("https://gitlab.com/group/repo.git", "tok-123")

    assert isinstance(auth.header, SecretStr)
    assert "tok-123" not in repr(auth)
    assert "oauth2" not in repr(auth)
    # But the real value is recoverable at the boundary.
    assert (
        "oauth2:tok-123"
        in base64.b64decode(auth.as_env()["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")).decode()
    )
