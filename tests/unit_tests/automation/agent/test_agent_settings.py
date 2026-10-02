import logging
from unittest.mock import patch

import pytest

from automation.agent.agent_settings import ModelChain, RunOverrides, resolve_agent_settings, resolve_features
from automation.agent.validators import AgentConfigurationError
from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings
from tests.unit_tests.automation.agent.agent_settings_cases import AGENT_SETTINGS_CASES
from tests.unit_tests.conftest import site_snapshot

# What the global site settings hold while a repository config is built; its default factories copy these in, so a
# resolver that took an unset ``.daiv.yml`` value from the config instead of the snapshot would return them.
AMBIENT_SITE = {
    "agent_model_name": "ambient-model",
    "agent_fallback_model_name": "ambient-fallback",
    "agent_thinking_level": "minimal",
    "diff_to_metadata_model_name": "ambient-d2m",
    "diff_to_metadata_fallback_model_name": "ambient-d2m-fallback",
    "pipeline_watch_max_attempts": 1,
}

BRANCHES = {
    "exact": RunOverrides(model_names=("exact-a", "exact-b"), agent_thinking_level="minimal"),
    "override": RunOverrides(agent_model="run-model", agent_thinking_level="minimal"),
    "max": RunOverrides(use_max=True, agent_thinking_level="minimal"),
    "default": RunOverrides(agent_thinking_level="minimal"),
}


def _repo(**daiv_yml) -> RepositoryConfig:
    with patch.multiple(site_settings, **AMBIENT_SITE):
        return RepositoryConfig(**daiv_yml)


def _resolve(*, site=None, repo=None, run=None):
    return resolve_agent_settings(site=site or site_snapshot(), repo=repo or _repo(), run=run or RunOverrides())


@pytest.mark.parametrize("case", AGENT_SETTINGS_CASES, ids=lambda case: case.id)
def test_each_case_resolves_its_chain_and_thinking_level(case):
    """Rows 1 and 2, the exact chain included. ``-d1`` cases pin D1, ``-through-raw`` cases pin D11."""
    site = site_snapshot(**case.site)
    repo = _repo(models={"agent": case.repo_agent})
    run = RunOverrides(**case.run)
    if case.raises:
        with pytest.raises(AgentConfigurationError):
            resolve_agent_settings(site=site, repo=repo, run=run)
        return

    agent = resolve_agent_settings(site=site, repo=repo, run=run).agent

    assert (agent.names, agent.thinking_level) == (case.chain, case.thinking_level)


def test_an_exact_chain_beats_a_run_override_and_max():
    run = RunOverrides(
        model_names=("exact-a", "exact-b"), agent_thinking_level="high", agent_model="run-model", use_max=True
    )

    agent = _resolve(site=site_snapshot(agent_max_model_name="site-max-model"), run=run).agent

    assert (agent.names, agent.thinking_level) == (("exact-a", "exact-b"), "high")


@pytest.mark.parametrize(("site_level", "warns"), [("bogus", True), ("", False)], ids=["invalid", "empty"])
def test_a_site_level_an_override_inherits_disables_thinking_when_unusable(caplog, site_level, warns):
    with caplog.at_level(logging.WARNING, logger="daiv.agent"):
        agent = _resolve(site=site_snapshot(agent_thinking_level=site_level), run=RunOverrides(agent_model="m")).agent

    assert agent.thinking_level is None
    assert ("Invalid agent thinking level" in caplog.text) is warns


def test_a_repo_null_thinking_level_survives_the_repo_config_cache(mock_repo_client):
    mock_repo_client.get_repository_file.return_value = "models:\n  agent:\n    thinking_level: null\n"
    repo_id = "group/null-thinking"
    try:
        with patch.multiple(site_settings, **AMBIENT_SITE):
            RepositoryConfig.get_config(repo_id)
            cached = RepositoryConfig.get_config(repo_id)
    finally:
        RepositoryConfig.invalidate_cache(repo_id)

    agent = _resolve(
        site=site_snapshot(agent_thinking_level="medium"), repo=cached, run=RunOverrides(agent_model="m")
    ).agent

    assert mock_repo_client.get_repository_file.call_count == 1
    assert agent.thinking_level is None


@pytest.mark.parametrize("run", BRANCHES.values(), ids=list(BRANCHES))
def test_the_fallback_thinking_level_is_the_sites_on_every_branch(run):
    site = site_snapshot(
        agent_thinking_level="medium", agent_max_thinking_level="high", agent_fallback_thinking_level="low"
    )
    repo = _repo(models={"agent": {"thinking_level": "xhigh"}})

    assert _resolve(site=site, repo=repo, run=run).agent.fallback_thinking_level == "low"


def test_d11_an_invalid_site_fallback_thinking_level_passes_through_raw():
    site = site_snapshot(agent_fallback_thinking_level="bogus")

    assert _resolve(site=site).agent.fallback_thinking_level == "bogus"


@pytest.mark.parametrize("run", BRANCHES.values(), ids=list(BRANCHES))
def test_the_explore_chain_is_the_sites_whatever_the_run_chooses(run):
    site = site_snapshot(agent_explore_model_name="explore", agent_explore_fallback_model_name="explore-fallback")
    repo = _repo(models={"agent": {"model": "repo-model", "thinking_level": "high"}})

    assert _resolve(site=site, repo=repo, run=run).explore == ModelChain(names=("explore", "explore-fallback"))


def test_the_explore_chain_has_no_fallback_when_the_site_sets_none():
    site = site_snapshot(agent_explore_model_name="explore", agent_explore_fallback_model_name="")

    assert _resolve(site=site).explore == ModelChain(names=("explore",))


def test_d2_the_diff_to_metadata_chain_is_the_sites_whatever_the_repo_sets():
    site = site_snapshot(diff_to_metadata_model_name="site-d2m", diff_to_metadata_fallback_model_name="site-d2m-fb")
    repo = _repo(models={"diff_to_metadata": {"model": "repo-d2m", "fallback_model": "repo-d2m-fb"}})

    assert _resolve(site=site, repo=repo).diff_to_metadata == ModelChain(names=("site-d2m", "site-d2m-fb"))


def test_the_recursion_limit_is_the_sites():
    assert _resolve(site=site_snapshot(agent_recursion_limit=123)).recursion_limit == 123


@pytest.mark.parametrize("toggle", ["web_search_enabled", "web_fetch_enabled"])
@pytest.mark.parametrize(
    ("run_value", "site_value", "expected"),
    [(None, True, True), (None, False, False), (False, True, False), (True, False, True)],
)
def test_a_web_toggle_follows_the_run_then_the_site(toggle, run_value, site_value, expected):
    settings = _resolve(site=site_snapshot(**{toggle: site_value}), run=RunOverrides(**{toggle: run_value}))

    assert getattr(settings, toggle) is expected


@pytest.mark.parametrize(
    ("site_model", "repo_agent", "expected"),
    [
        ("consolidator", {"model": "repo-model", "fallback_model": "repo-fallback"}, ("consolidator", "repo-fallback")),
        (None, {"model": "repo-model", "fallback_model": "repo-fallback"}, ("repo-model", "repo-fallback")),
        (None, {}, ("site-model", "site-fallback")),
    ],
    ids=["site-model-wins", "repo-agent-model-otherwise", "site-agent-model-when-the-repo-sets-none"],
)
def test_the_consolidation_chain(site_model, repo_agent, expected):
    site = site_snapshot(
        memory_consolidation_model_name=site_model,
        agent_model_name="site-model",
        agent_fallback_model_name="site-fallback",
    )

    settings = _resolve(site=site, repo=_repo(models={"agent": repo_agent}))

    assert settings.consolidation == ModelChain(names=expected)


AND_SWITCHES = {
    "memory": ("memory_enabled", lambda on: {"memory": {"enabled": on}}),
    "suggest_context_file": ("suggest_context_file_enabled", lambda on: {"suggest_context_file": on}),
    "session_link": ("session_link_enabled", lambda on: {"session_link": on}),
    "pipeline_watch": ("pipeline_watch_enabled", lambda on: {"pipeline_watch": {"enabled": on}}),
}


@pytest.mark.parametrize("feature", AND_SWITCHES)
@pytest.mark.parametrize(("site_on", "repo_on"), [(True, True), (True, False), (False, True), (False, False)])
def test_a_switch_is_on_only_when_both_the_site_and_the_repo_turn_it_on(feature, site_on, repo_on):
    site_field, daiv_yml = AND_SWITCHES[feature]

    features = resolve_features(site=site_snapshot(**{site_field: site_on}), repo=_repo(**daiv_yml(repo_on)))

    assert getattr(features, feature) is (site_on and repo_on)


@pytest.mark.parametrize("repo_on", [True, False])
def test_slash_commands_follow_the_repo_whatever_the_site_switches_say(repo_on):
    site_off = {name: False for name, default in site_settings.FIELD_DEFAULTS.items() if isinstance(default, bool)}

    features = resolve_features(site=site_snapshot(**site_off), repo=_repo(slash_commands={"enabled": repo_on}))

    assert features.slash_commands is repo_on


@pytest.mark.parametrize(
    ("daiv_yml", "expected"),
    [({}, 3), ({"pipeline_watch": {"max_attempts": 2}}, 2), ({"pipeline_watch": {"max_attempts": 5}}, 3)],
    ids=["site-when-the-repo-sets-none", "a-repo-lowers-it", "a-repo-cannot-raise-it"],
)
def test_the_watch_attempt_cap_is_the_lower_of_the_repos_and_the_sites(daiv_yml, expected):
    features = resolve_features(site=site_snapshot(pipeline_watch_max_attempts=3), repo=_repo(**daiv_yml))

    assert features.pipeline_watch_max_attempts == expected


def test_the_settings_carry_the_resolved_features():
    site = site_snapshot(memory_enabled=False, pipeline_watch_max_attempts=4)
    repo = _repo(session_link=False, pipeline_watch={"max_attempts": 2})

    assert _resolve(site=site, repo=repo).features == resolve_features(site=site, repo=repo)
