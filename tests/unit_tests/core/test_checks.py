from core.checks import REMOVED_ENV_VARS, check_removed_env_vars


def test_a_removed_env_var_still_set_is_warned_about(monkeypatch):
    monkeypatch.setenv("DAIV_AGENT_EXPLORE_MODEL_NAME", "anthropic:claude-haiku-5.5")
    monkeypatch.delenv("DAIV_AGENT_EXPLORE_FALLBACK_MODEL_NAME", raising=False)

    [warning] = check_removed_env_vars(None)

    assert (warning.id, warning.msg) == ("core.W001", "DAIV_AGENT_EXPLORE_MODEL_NAME is no longer read.")
    assert "DAIV_AGENT_SUBAGENT_MODELS" in warning.hint


def test_nothing_is_warned_about_when_no_removed_env_var_is_set(monkeypatch):
    for name in REMOVED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    assert check_removed_env_vars(None) == []
