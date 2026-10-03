import logging
from unittest.mock import patch

import pytest

from automation.agent.agent_settings import (
    ModelChain,
    RunOverrides,
    resolve_agent_settings,
    resolve_consolidation_chain,
    resolve_features,
)
from automation.agent.validators import AgentConfigurationError
from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings
from tests.unit_tests.automation.agent.agent_settings_cases import AGENT_SETTINGS_CASES
from tests.unit_tests.conftest import site_snapshot

BRANCHES = {
    "exact": RunOverrides(model_names=("exact-a", "exact-b"), agent_thinking_level="minimal"),
    "override": RunOverrides(agent_model="run-model", agent_thinking_level="minimal"),
    "max": RunOverrides(use_max=True, agent_thinking_level="minimal"),
    "default": RunOverrides(agent_thinking_level="minimal"),
}


def _resolve(*, site=None, repo=None, run=None):
    return resolve_agent_settings(
        site=site or site_snapshot(), repo=repo or RepositoryConfig(), run=run or RunOverrides()
    )


@pytest.mark.parametrize("case", AGENT_SETTINGS_CASES, ids=lambda case: case.id)
def test_each_case_resolves_its_chain_and_thinking_level(case):
    """Rows 1 and 2, the exact chain included. ``-through-raw`` cases pin D11."""
    site = site_snapshot(**case.site)
    repo = RepositoryConfig(models={"agent": case.repo_agent})
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
        RepositoryConfig.get_config(repo_id)
        cached = RepositoryConfig.get_config(repo_id)
    finally:
        RepositoryConfig.invalidate_cache(repo_id)

    agent = _resolve(
        site=site_snapshot(agent_thinking_level="medium"), repo=cached, run=RunOverrides(agent_model="m")
    ).agent

    assert mock_repo_client.get_repository_file.call_count == 1
    assert agent.thinking_level is None


@pytest.mark.parametrize(
    ("entry", "expected_chain", "expected_thinking_level", "expected_attempts"),
    [
        (
            {
                "default_branch": "main",
                "pipeline_watch": {"max_attempts": 2},
                "models": {"agent": {"model": "repo-model", "thinking_level": None}},
            },
            ("run-model", "repo-model", "site-fallback"),
            None,
            2,
        ),
        ({"default_branch": "main"}, ("run-model", "site-model", "site-fallback"), "medium", 3),
    ],
    ids=["the-fields-a-repo-set", "nothing-set"],
)
def test_a_cache_entry_written_before_the_config_stopped_copying_the_site_resolves_the_same(
    entry, expected_chain, expected_thinking_level, expected_attempts
):
    """Entries hold only what a repo set, so they load as they did and the resolver fills in the rest from the site."""
    with patch("codebase.repo_config.cache") as cache:
        cache.get.return_value = entry
        repo = RepositoryConfig.get_config("group/old-entry")
    site = site_snapshot(
        agent_model_name="site-model",
        agent_fallback_model_name="site-fallback",
        agent_thinking_level="medium",
        pipeline_watch_max_attempts=3,
    )

    settings = _resolve(site=site, repo=repo, run=RunOverrides(agent_model="run-model"))

    assert (settings.agent.names, settings.agent.thinking_level) == (expected_chain, expected_thinking_level)
    assert settings.features.pipeline_watch_max_attempts == expected_attempts


def test_a_null_model_or_attempt_cap_in_the_repo_is_unset():
    repo = RepositoryConfig(
        models={"agent": {"model": None, "fallback_model": None}}, pipeline_watch={"max_attempts": None}
    )
    site = site_snapshot(
        agent_model_name="site-model", agent_fallback_model_name="site-fallback", pipeline_watch_max_attempts=3
    )

    settings = _resolve(site=site, repo=repo, run=RunOverrides(agent_model="run-model"))

    assert settings.agent.names == ("run-model", "site-model", "site-fallback")
    assert settings.features.pipeline_watch_max_attempts == 3


@pytest.mark.parametrize("run", BRANCHES.values(), ids=list(BRANCHES))
def test_the_fallback_thinking_level_is_the_sites_on_every_branch(run):
    site = site_snapshot(
        agent_thinking_level="medium", agent_max_thinking_level="high", agent_fallback_thinking_level="low"
    )
    repo = RepositoryConfig(models={"agent": {"thinking_level": "xhigh"}})

    assert _resolve(site=site, repo=repo, run=run).agent.fallback_thinking_level == "low"


def test_d11_an_invalid_site_fallback_thinking_level_passes_through_raw():
    site = site_snapshot(agent_fallback_thinking_level="bogus")

    assert _resolve(site=site).agent.fallback_thinking_level == "bogus"


@pytest.mark.parametrize("run", BRANCHES.values(), ids=list(BRANCHES))
def test_the_explore_chain_is_the_sites_whatever_the_run_chooses(run):
    site = site_snapshot(agent_explore_model_name="explore", agent_explore_fallback_model_name="explore-fallback")
    repo = RepositoryConfig(models={"agent": {"model": "repo-model", "thinking_level": "high"}})

    assert _resolve(site=site, repo=repo, run=run).explore == ModelChain(names=("explore", "explore-fallback"))


def test_the_explore_chain_has_no_fallback_when_the_site_sets_none():
    site = site_snapshot(agent_explore_model_name="explore", agent_explore_fallback_model_name="")

    assert _resolve(site=site).explore == ModelChain(names=("explore",))


def test_d2_the_diff_to_metadata_chain_is_the_sites_whatever_the_repo_sets():
    site = site_snapshot(diff_to_metadata_model_name="site-d2m", diff_to_metadata_fallback_model_name="site-d2m-fb")
    repo = RepositoryConfig(models={"diff_to_metadata": {"model": "repo-d2m", "fallback_model": "repo-d2m-fb"}})

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
        (None, {"model": "", "fallback_model": ""}, ("site-model", "site-fallback")),
    ],
    ids=[
        "site-model-wins",
        "repo-agent-model-otherwise",
        "site-agent-model-when-the-repo-sets-none",
        "site-agent-model-when-the-repo-sets-blanks",
    ],
)
def test_the_consolidation_chain(site_model, repo_agent, expected):
    site = site_snapshot(
        memory_consolidation_model_name=site_model,
        agent_model_name="site-model",
        agent_fallback_model_name="site-fallback",
    )

    repo = RepositoryConfig(models={"agent": repo_agent})

    settings = _resolve(site=site, repo=repo)

    assert settings.consolidation == ModelChain(names=expected)
    assert resolve_consolidation_chain(site=site, repo=repo) == settings.consolidation


def test_the_consolidation_chain_resolves_alone_where_the_agent_chain_would_raise():
    site = site_snapshot(
        agent_model_name="", agent_fallback_model_name="", memory_consolidation_model_name="consolidator"
    )
    repo = RepositoryConfig(models={"agent": {"fallback_model": "repo-fallback"}})

    with pytest.raises(AgentConfigurationError):
        resolve_agent_settings(site=site, repo=repo, run=RunOverrides())

    assert resolve_consolidation_chain(site=site, repo=repo) == ModelChain(names=("consolidator", "repo-fallback"))


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

    features = resolve_features(site=site_snapshot(**{site_field: site_on}), repo=RepositoryConfig(**daiv_yml(repo_on)))

    assert getattr(features, feature) is (site_on and repo_on)


@pytest.mark.parametrize("repo_on", [True, False])
def test_slash_commands_follow_the_repo_whatever_the_site_switches_say(repo_on):
    site_off = {name: False for name, default in site_settings.FIELD_DEFAULTS.items() if isinstance(default, bool)}

    features = resolve_features(
        site=site_snapshot(**site_off), repo=RepositoryConfig(slash_commands={"enabled": repo_on})
    )

    assert features.slash_commands is repo_on


@pytest.mark.parametrize(
    ("daiv_yml", "expected"),
    [
        ({}, 3),
        ({"pipeline_watch": {"max_attempts": 2}}, 2),
        ({"pipeline_watch": {"max_attempts": 0}}, 0),
        ({"pipeline_watch": {"max_attempts": 5}}, 3),
    ],
    ids=["site-when-the-repo-sets-none", "a-repo-lowers-it", "a-repo-zero-is-a-cap", "a-repo-cannot-raise-it"],
)
def test_the_watch_attempt_cap_is_the_lower_of_the_repos_and_the_sites(daiv_yml, expected):
    features = resolve_features(site=site_snapshot(pipeline_watch_max_attempts=3), repo=RepositoryConfig(**daiv_yml))

    assert features.pipeline_watch_max_attempts == expected


def test_the_settings_carry_the_resolved_features():
    site = site_snapshot(memory_enabled=False, pipeline_watch_max_attempts=4)
    repo = RepositoryConfig(session_link=False, pipeline_watch={"max_attempts": 2})

    assert _resolve(site=site, repo=repo).features == resolve_features(site=site, repo=repo)
