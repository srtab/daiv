import logging
from unittest.mock import patch

import pytest

from automation.agent.agent_settings import (
    ModelChain,
    RunOverrides,
    resolve_agent_settings,
    resolve_consolidation_chain,
    resolve_features,
    resolve_pipeline_watch_enabled,
    resolve_pipeline_watch_max_attempts,
)
from automation.agent.validators import AgentConfigurationError
from codebase.repo_config import RepositoryConfig
from core.models import SiteConfiguration
from core.site_settings import site_settings
from tests.unit_tests.automation.agent.agent_settings_cases import AGENT_SETTINGS_CASES
from tests.unit_tests.conftest import agent_settings, site_snapshot

BRANCHES = {
    "exact": RunOverrides(model_names=("exact-a", "exact-b"), agent_thinking_level="minimal"),
    "override": RunOverrides(agent_model="run-model", agent_thinking_level="minimal"),
    "max": RunOverrides(use_max=True, agent_thinking_level="minimal"),
    "default": RunOverrides(agent_thinking_level="minimal"),
}


@pytest.mark.parametrize("case", AGENT_SETTINGS_CASES, ids=lambda case: case.id)
def test_each_case_resolves_its_chain_and_thinking_level(case):
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

    agent = agent_settings(site=site_snapshot(agent_max_model_name="site-max-model"), run=run).agent

    assert (agent.names, agent.thinking_level) == (("exact-a", "exact-b"), "high")


@pytest.mark.parametrize(
    ("site_field", "run", "chain_field"),
    [
        ("agent_thinking_level", RunOverrides(), "thinking_level"),
        ("agent_thinking_level", RunOverrides(agent_model="m"), "thinking_level"),
        ("agent_max_thinking_level", RunOverrides(use_max=True), "thinking_level"),
        ("agent_fallback_thinking_level", RunOverrides(), "fallback_thinking_level"),
    ],
    ids=["default", "override", "max", "fallback"],
)
@pytest.mark.parametrize(("site_level", "logs"), [("bogus", True), ("", False)], ids=["invalid", "empty"])
def test_an_unusable_site_thinking_level_disables_thinking(caplog, site_field, run, chain_field, site_level, logs):
    with caplog.at_level(logging.ERROR, logger="daiv.agent"):
        agent = agent_settings(site=site_snapshot(**{site_field: site_level}), run=run).agent

    assert getattr(agent, chain_field) is None
    assert ("Invalid thinking level 'bogus'" in caplog.text) is logs


def test_a_repo_null_thinking_level_survives_the_repo_config_cache(mock_repo_client):
    mock_repo_client.get_repository_file.return_value = "models:\n  agent:\n    thinking_level: null\n"
    repo_id = "group/null-thinking"
    try:
        RepositoryConfig.get_config(repo_id)
        cached = RepositoryConfig.get_config(repo_id)
    finally:
        RepositoryConfig.invalidate_cache(repo_id)

    agent = agent_settings(
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
def test_a_cached_entry_holds_only_what_the_repo_set(entry, expected_chain, expected_thinking_level, expected_attempts):
    with patch("codebase.repo_config.cache") as cache:
        cache.get.return_value = entry
        repo = RepositoryConfig.get_config("group/old-entry")
    site = site_snapshot(
        agent_model_name="site-model",
        agent_fallback_model_name="site-fallback",
        agent_thinking_level="medium",
        pipeline_watch_max_attempts=3,
    )

    agent = agent_settings(site=site, repo=repo, run=RunOverrides(agent_model="run-model")).agent

    assert (agent.names, agent.thinking_level) == (expected_chain, expected_thinking_level)
    assert resolve_pipeline_watch_max_attempts(site=site, repo=repo) == expected_attempts


def test_a_null_model_or_attempt_cap_in_the_repo_is_unset():
    repo = RepositoryConfig(
        models={"agent": {"model": None, "fallback_model": None}}, pipeline_watch={"max_attempts": None}
    )
    site = site_snapshot(
        agent_model_name="site-model", agent_fallback_model_name="site-fallback", pipeline_watch_max_attempts=3
    )

    agent = agent_settings(site=site, repo=repo, run=RunOverrides(agent_model="run-model")).agent

    assert agent.names == ("run-model", "site-model", "site-fallback")
    assert resolve_pipeline_watch_max_attempts(site=site, repo=repo) == 3


@pytest.mark.parametrize("run", BRANCHES.values(), ids=list(BRANCHES))
def test_the_fallback_thinking_level_is_the_sites_on_every_branch(run):
    site = site_snapshot(
        agent_thinking_level="medium", agent_max_thinking_level="high", agent_fallback_thinking_level="low"
    )
    repo = RepositoryConfig(models={"agent": {"thinking_level": "xhigh"}})

    assert agent_settings(site=site, repo=repo, run=run).agent.fallback_thinking_level == "low"


@pytest.mark.parametrize("run", BRANCHES.values(), ids=list(BRANCHES))
def test_the_explore_chain_is_the_sites_whatever_the_run_chooses(run):
    site = site_snapshot(agent_explore_model_name="explore", agent_explore_fallback_model_name="explore-fallback")
    repo = RepositoryConfig(models={"agent": {"model": "repo-model", "thinking_level": "high"}})

    assert agent_settings(site=site, repo=repo, run=run).explore == ModelChain(names=("explore", "explore-fallback"))


def test_the_explore_chain_has_no_fallback_when_the_site_sets_none():
    site = site_snapshot(agent_explore_model_name="explore", agent_explore_fallback_model_name="")

    assert agent_settings(site=site).explore == ModelChain(names=("explore",))


@pytest.mark.parametrize(
    ("repo_d2m", "expected"),
    [
        ({"model": "repo-d2m", "fallback_model": "repo-d2m-fb"}, ("repo-d2m", "repo-d2m-fb")),
        ({"model": "repo-d2m"}, ("repo-d2m", "site-d2m-fb")),
        ({}, ("site-d2m", "site-d2m-fb")),
        ({"model": "", "fallback_model": None}, ("site-d2m", "site-d2m-fb")),
    ],
    ids=["repo-sets-both", "repo-sets-only-the-model", "repo-sets-none", "repo-sets-blanks"],
)
def test_the_diff_to_metadata_chain_is_the_repos_else_the_sites(repo_d2m, expected):
    site = site_snapshot(diff_to_metadata_model_name="site-d2m", diff_to_metadata_fallback_model_name="site-d2m-fb")
    repo = RepositoryConfig(models={"diff_to_metadata": repo_d2m})

    assert agent_settings(site=site, repo=repo).diff_to_metadata == ModelChain(names=expected)


def test_the_diff_to_metadata_chain_drops_a_blank_site_fallback():
    site = site_snapshot(diff_to_metadata_model_name="site-d2m", diff_to_metadata_fallback_model_name="")

    assert agent_settings(site=site).diff_to_metadata == ModelChain(names=("site-d2m",))


def test_the_recursion_limit_is_the_sites():
    assert agent_settings(site=site_snapshot(agent_recursion_limit=123)).recursion_limit == 123


@pytest.mark.parametrize("toggle", ["web_search_enabled", "web_fetch_enabled"])
@pytest.mark.parametrize(
    ("run_value", "site_value", "expected"),
    [(None, True, True), (None, False, False), (False, True, False), (True, False, True)],
)
def test_a_web_toggle_follows_the_run_then_the_site(toggle, run_value, site_value, expected):
    settings = agent_settings(site=site_snapshot(**{toggle: site_value}), run=RunOverrides(**{toggle: run_value}))

    assert getattr(settings, toggle) is expected


@pytest.mark.parametrize(
    ("allowed", "site_value", "expected"), [(True, True, True), (True, False, False), (False, True, False)]
)
def test_cross_project_access_needs_both_the_site_and_the_run(allowed, site_value, expected):
    settings = agent_settings(
        site=site_snapshot(cross_project_access_enabled=site_value), run=RunOverrides(cross_project_allowed=allowed)
    )

    assert settings.cross_project_enabled is expected


def test_a_run_that_asks_nothing_gets_no_cross_project_access():
    assert agent_settings(site=site_snapshot(cross_project_access_enabled=True)).cross_project_enabled is False


@pytest.mark.parametrize(
    ("site_model", "repo_agent", "expected"),
    [
        ("consolidator", {"model": "repo-model", "fallback_model": "repo-fallback"}, ("consolidator", "repo-fallback")),
        (None, {"model": "repo-model", "fallback_model": "repo-fallback"}, ("repo-model", "repo-fallback")),
        (None, {}, ("site-model", "site-fallback")),
        (None, {"model": "", "fallback_model": ""}, ("site-model", "site-fallback")),
        ("", {}, ("site-model", "site-fallback")),
    ],
    ids=[
        "site-model-wins",
        "repo-agent-model-otherwise",
        "site-agent-model-when-the-repo-sets-none",
        "site-agent-model-when-the-repo-sets-blanks",
        "a-blank-site-model-is-unset",
    ],
)
def test_the_consolidation_chain(site_model, repo_agent, expected):
    site = site_snapshot(
        memory_consolidation_model_name=site_model,
        agent_model_name="site-model",
        agent_fallback_model_name="site-fallback",
    )
    repo = RepositoryConfig(models={"agent": repo_agent})

    assert resolve_consolidation_chain(site=site, repo=repo) == ModelChain(names=expected)


def test_the_consolidation_chain_resolves_alone_where_the_agent_chain_would_raise():
    site = site_snapshot(
        agent_model_name="", agent_fallback_model_name="", memory_consolidation_model_name="consolidator"
    )
    repo = RepositoryConfig(models={"agent": {"fallback_model": "repo-fallback"}})

    with pytest.raises(AgentConfigurationError):
        resolve_agent_settings(site=site, repo=repo, run=RunOverrides())

    assert resolve_consolidation_chain(site=site, repo=repo) == ModelChain(names=("consolidator", "repo-fallback"))


def _feature(name):
    return lambda site, repo: getattr(resolve_features(site=site, repo=repo), name)


AND_SWITCHES = {
    "memory": ("memory_enabled", lambda on: {"memory": {"enabled": on}}, _feature("memory")),
    "suggest_context_file": (
        "suggest_context_file_enabled",
        lambda on: {"suggest_context_file": on},
        _feature("suggest_context_file"),
    ),
    "session_link": ("session_link_enabled", lambda on: {"session_link": on}, _feature("session_link")),
    "pipeline_watch": (
        "pipeline_watch_enabled",
        lambda on: {"pipeline_watch": {"enabled": on}},
        resolve_pipeline_watch_enabled,
    ),
}


@pytest.mark.parametrize("switch", AND_SWITCHES)
@pytest.mark.parametrize(("site_on", "repo_on"), [(True, True), (True, False), (False, True), (False, False)])
def test_a_switch_is_on_only_when_both_the_site_and_the_repo_turn_it_on(switch, site_on, repo_on):
    site_field, daiv_yml, read = AND_SWITCHES[switch]

    is_on = read(site=site_snapshot(**{site_field: site_on}), repo=RepositoryConfig(**daiv_yml(repo_on)))

    assert is_on is (site_on and repo_on)


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
    site = site_snapshot(pipeline_watch_max_attempts=3)

    assert resolve_pipeline_watch_max_attempts(site=site, repo=RepositoryConfig(**daiv_yml)) == expected


def test_the_settings_carry_the_resolved_features():
    site = site_snapshot(memory_enabled=False)
    repo = RepositoryConfig(session_link=False)

    assert agent_settings(site=site, repo=repo).features == resolve_features(site=site, repo=repo)


def test_a_malformed_watch_attempt_cap_fails_only_the_cap(monkeypatch):
    monkeypatch.setenv("DAIV_PIPELINE_WATCH_MAX_ATTEMPTS", "3x")
    with patch.object(SiteConfiguration, "get_cached", return_value=None):
        site = site_settings.snapshot()
    repo = RepositoryConfig(pipeline_watch={"max_attempts": 2})

    assert agent_settings(site=site, repo=repo, run=RunOverrides(agent_model="m")).agent.names[0] == "m"
    assert resolve_features(site=site, repo=repo).memory is True
    assert resolve_pipeline_watch_enabled(site=site, repo=repo) is True
    with pytest.raises(ValueError, match="Cannot parse"):
        resolve_pipeline_watch_max_attempts(site=site, repo=repo)
