"""The settings one agent run gets, resolved once from the site, the repository's ``.daiv.yml`` and the run.

Precedence, row for row with the config-resolver plan's Today table. Inputs are listed highest first; for the model
chain the first matching branch wins. "Repo" is a value ``.daiv.yml`` sets (in ``model_fields_set``; a null model or
attempt cap counts as unset), else the site's.

 1. Agent model chain: exact chain (``model_names``) → run override: ``[override, repo model, repo fallback]`` →
    ``use_max``: ``[site max, repo model, repo fallback]`` → default: ``[site model, site fallback]``, raising
    ``AgentConfigurationError`` when the site has no model.
 2. Agent thinking level: exact: as given · override: run, then repo · max: site max, run value ignored · default:
    run, then site.
 3. Fallback-model thinking: site ``agent_fallback_thinking_level``, every branch.
 4. Explore subagent chain: site only, without a fallback when the site sets none.
 5. Diff-to-metadata chain: site only; ``.daiv.yml`` does not choose it (D2).
 6. Recursion limit: site ``agent_recursion_limit``; chat's call-time 500 stays at its call site (D5).
 7. Web search, web fetch: the run's option, then site.
 8. Memory: site AND repo.
 9. Consolidation chain: site ``memory_consolidation_model_name``, then the repo agent model; then the repo fallback.
    ``resolve_consolidation_chain`` gives it alone and never raises, for consolidation outside a run.
10. Suggest context file, session link: site AND repo; the session link's thread check stays at its call site.
11. Slash commands: repo only.
12. Pipeline watch: enabled: site AND repo · attempts: min(repo, site).

Site thinking levels reach the model unvalidated (D11), so ``ModelChain`` levels may be raw ``str``; only the site level
an override inherits as the repo default is coerced to ``None``, with a warning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.models import ThinkingLevelChoices as ThinkingLevel

if TYPE_CHECKING:
    from pydantic import BaseModel

    from codebase.repo_config import AgentModelConfig, RepositoryConfig
    from core.site_settings import SiteSnapshot

logger = logging.getLogger("daiv.agent")


@dataclass(frozen=True)
class RunOverrides:
    agent_model: str | None = None
    agent_thinking_level: str | None = None
    use_max: bool = False
    model_names: tuple[str, ...] = ()
    web_search_enabled: bool | None = None
    web_fetch_enabled: bool | None = None


@dataclass(frozen=True)
class ModelChain:
    names: tuple[str, ...]
    thinking_level: ThinkingLevel | str | None = None
    fallback_thinking_level: ThinkingLevel | str | None = None


@dataclass(frozen=True)
class Features:
    memory: bool
    suggest_context_file: bool
    session_link: bool
    slash_commands: bool
    pipeline_watch: bool
    pipeline_watch_max_attempts: int


@dataclass(frozen=True)
class AgentSettings:
    agent: ModelChain
    explore: ModelChain
    diff_to_metadata: ModelChain
    consolidation: ModelChain
    recursion_limit: int
    web_search_enabled: bool
    web_fetch_enabled: bool
    features: Features


def resolve_features(*, site: SiteSnapshot, repo: RepositoryConfig) -> Features:
    """Rows 8 and 10–12: the switches that combine a site setting with ``.daiv.yml``."""
    attempts = _repo_value(repo.pipeline_watch, "max_attempts", site.pipeline_watch_max_attempts)
    return Features(
        memory=repo.memory.enabled and site.memory_enabled,
        suggest_context_file=repo.suggest_context_file and site.suggest_context_file_enabled,
        session_link=repo.session_link and site.session_link_enabled,
        slash_commands=repo.slash_commands.enabled,
        pipeline_watch=repo.pipeline_watch.enabled and site.pipeline_watch_enabled,
        pipeline_watch_max_attempts=min(attempts, site.pipeline_watch_max_attempts),
    )


def resolve_agent_settings(*, site: SiteSnapshot, repo: RepositoryConfig, run: RunOverrides) -> AgentSettings:
    """Every per-run agent setting, by the precedence in this module's docstring."""
    repo_agent = repo.models.agent
    explore_names: tuple[str, ...] = (site.agent_explore_model_name,)
    if site.agent_explore_fallback_model_name:
        explore_names += (site.agent_explore_fallback_model_name,)
    return AgentSettings(
        agent=_resolve_agent(site=site, repo_agent=repo_agent, repo_chain=_repo_models(site, repo_agent), run=run),
        explore=ModelChain(names=explore_names),
        diff_to_metadata=ModelChain(
            names=(site.diff_to_metadata_model_name, site.diff_to_metadata_fallback_model_name)
        ),
        consolidation=resolve_consolidation_chain(site=site, repo=repo),
        recursion_limit=site.agent_recursion_limit,
        web_search_enabled=_run_or_site(run.web_search_enabled, site.web_search_enabled),
        web_fetch_enabled=_run_or_site(run.web_fetch_enabled, site.web_fetch_enabled),
        features=resolve_features(site=site, repo=repo),
    )


def resolve_consolidation_chain(*, site: SiteSnapshot, repo: RepositoryConfig) -> ModelChain:
    """Row 9 on its own: unlike ``resolve_agent_settings`` it never raises, so a repo with its own agent model
    still consolidates when the site has no default one."""
    repo_model, repo_fallback = _repo_models(site, repo.models.agent)
    return ModelChain(names=(site.memory_consolidation_model_name or repo_model, repo_fallback))


def _repo_models(site: SiteSnapshot, repo_agent: AgentModelConfig) -> tuple[str, str]:
    return (
        _repo_value(repo_agent, "model", site.agent_model_name),
        _repo_value(repo_agent, "fallback_model", site.agent_fallback_model_name),
    )


def _resolve_agent(
    *, site: SiteSnapshot, repo_agent: AgentModelConfig, repo_chain: tuple[str, str], run: RunOverrides
) -> ModelChain:
    fallback_thinking_level = site.agent_fallback_thinking_level
    if run.model_names:
        return ModelChain(
            names=run.model_names,
            thinking_level=run.agent_thinking_level,
            fallback_thinking_level=fallback_thinking_level,
        )
    if run.agent_model:
        if "thinking_level" in repo_agent.model_fields_set:
            repo_thinking_level = repo_agent.thinking_level
        else:
            repo_thinking_level = _coerce_site_thinking_level(site.agent_thinking_level)
        return ModelChain(
            names=(run.agent_model, *repo_chain),
            thinking_level=run.agent_thinking_level or repo_thinking_level,
            fallback_thinking_level=fallback_thinking_level,
        )
    if run.use_max:
        return ModelChain(
            names=(site.agent_max_model_name, *repo_chain),
            thinking_level=site.agent_max_thinking_level,
            fallback_thinking_level=fallback_thinking_level,
        )
    if not site.agent_model_name:
        # Imported here: validators pulls in the agent stack, which the memory tasks and the watch policy must not load.
        from .validators import AgentConfigurationError

        raise AgentConfigurationError(
            "No agent model configured. Set the system default (DAIV_AGENT_MODEL_NAME / "
            "site settings) or pass an explicit `agent_model` override."
        )
    return ModelChain(
        names=(site.agent_model_name, site.agent_fallback_model_name),
        thinking_level=run.agent_thinking_level or site.agent_thinking_level,
        fallback_thinking_level=fallback_thinking_level,
    )


def _coerce_site_thinking_level(raw: str) -> ThinkingLevel | None:
    if not raw:
        return None
    try:
        return ThinkingLevel(raw)
    except ValueError:
        logger.warning("Invalid agent thinking level %r in site settings; ignoring it.", raw)
        return None


def _repo_value[T](section: BaseModel, field: str, site_value: T) -> T:
    if field in section.model_fields_set and (value := getattr(section, field)) is not None:
        return value
    return site_value


def _run_or_site[T](run_value: T | None, site_value: T) -> T:
    return site_value if run_value is None else run_value
