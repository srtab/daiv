import base64

from codebase.clients.utils import _clean_ansi_codes, git_auth_env


def test_git_auth_env_builds_host_scoped_extraheader_config():
    """The env must authenticate git-over-HTTPS without the token ever reaching argv or .git/config:
    a command-scoped http.<origin>.extraheader plus fully disabled prompting (so a *rejected*
    credential fails fast with git's 'could not read Username' auth marker instead of hanging).
    Both GIT_TERMINAL_PROMPT=0 and GIT_ASKPASS='' are required: the empty GIT_ASKPASS short-circuits
    git's fallback to an inherited SSH_ASKPASS GUI helper, which TERMINAL_PROMPT alone does not."""
    env = git_auth_env("https://gitlab.com/group/repo.git", "tok-123")

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
    env = git_auth_env("http://gitlab.local:8080/group/repo.git", "tok")

    assert env["GIT_CONFIG_KEY_0"] == "http.http://gitlab.local:8080/.extraheader"


def test__clean_ansi_codes():
    """Test that _clean_gitlab_logs properly cleans GitLab logs."""
    raw_log = (
        "\x1b[0msection_start:123: step_script\r\n"
        "Running command\x1b[0m\r\n"
        "Output with\rcarriage return\r\n"
        "\x1b[32mColored text\x1b[0m\n"
        "section_end:123: step_script"
    )

    result = _clean_ansi_codes(raw_log)

    assert "\x1b[" not in result  # No ANSI codes
    assert "\r\n" not in result  # No Windows line endings


def test__clean_ansi_codes_empty_log():
    """Test that _clean_ansi_codes handles empty logs."""
    assert _clean_ansi_codes("") == ""
