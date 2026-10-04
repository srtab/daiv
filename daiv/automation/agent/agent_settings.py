"""The settings one agent run gets, resolved once from the site, the repository's ``.daiv.yml`` and the run.

This docstring is the precedence table. Inputs are listed highest first; for the model chain the first matching branch
wins. "Repo" is a value ``.daiv.yml`` sets (in ``model_fields_set``; a null or empty model or fallback and a null
attempt cap count as unset), else the site's. Every chain drops blank model names.

 1. Agent model chain: exact chain (``model_names``) → run override: ``[override, repo model, repo fallback]`` →
    ``use_max``: ``[site max, repo model, repo fallback]`` → default: ``[repo model, repo fallback]``, raising
    ``AgentConfigurationError`` when neither the repo nor the site has a model, or a branch is left with no model.
 2. Agent thinking level: exact: the run's · override and default: run, then repo (a null repo level disables
    thinking) · max: site max, run value ignored.
 3. Fallback-model thinking: site ``agent_fallback_thinking_level``, every branch.
 4. Explore subagent chain: site only.
 5. Diff-to-metadata chain: ``[repo model, repo fallback]`` from ``models.diff_to_metadata``.
 6. Recursion limit: site ``agent_recursion_limit``; chat passes 500 at call time, which beats it.
 7. Web search, web fetch: the run's option, then site.
 8. Memory: site AND repo.
 9. Consolidation chain: ``[site memory_consolidation_model_name or repo model, repo fallback]``. Outside
    ``AgentSettings``: consolidation runs outside a run, through ``resolve_consolidation_chain``, which never raises.
10. Suggest context file, session link: site AND repo; the session link's thread check stays at its call site.
11. Slash commands: repo only.
12. Pipeline watch: enabled: site AND repo · attempts: min(repo, site). Outside ``AgentSettings``: ``WatchPolicy``
    resolves them when the watch arms, so a run never reads them.

A site or run thinking level that is not a ``ThinkingLevel`` becomes ``None`` (no thinking) and is logged as an error;
``.daiv.yml`` levels are validated when the file loads.
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

    from .validators import AgentConfigurationError

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
    thinking_level: ThinkingLevel | None = None
    fallback_thinking_level: ThinkingLevel | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "names", tuple(name for name in self.names if name))


@dataclass(frozen=True)
class Features:
    memory: bool
    suggest_context_file: bool
    session_link: bool
    slash_commands: bool


@dataclass(frozen=True)
class AgentSettings:
    agent: ModelChain
    explore: ModelChain
    diff_to_metadata: ModelChain
    recursion_limit: int
    web_search_enabled: bool
    web_fetch_enabled: bool
    features: Features


def resolve_agent_settings(*, site: SiteSnapshot, repo: RepositoryConfig, run: RunOverrides) -> AgentSettings:
    """Every per-run agent setting, by the precedence in this module's docstring."""
    return AgentSettings(
        agent=_resolve_agent(site=site, repo_agent=repo.models.agent, run=run),
        explore=ModelChain(names=(site.agent_explore_model_name, site.agent_explore_fallback_model_name)),
        diff_to_metadata=ModelChain(
            names=_repo_chain(
                repo.models.diff_to_metadata,
                site_model=site.diff_to_metadata_model_name,
                site_fallback=site.diff_to_metadata_fallback_model_name,
            )
        ),
        recursion_limit=site.agent_recursion_limit,
        web_search_enabled=_run_or_site(run.web_search_enabled, site.web_search_enabled),
        web_fetch_enabled=_run_or_site(run.web_fetch_enabled, site.web_fetch_enabled),
        features=resolve_features(site=site, repo=repo),
    )


def resolve_features(*, site: SiteSnapshot, repo: RepositoryConfig) -> Features:
    """Rows 8, 10 and 11: the run's feature switches."""
    return Features(
        memory=repo.memory.enabled and site.memory_enabled,
        suggest_context_file=repo.suggest_context_file and site.suggest_context_file_enabled,
        session_link=repo.session_link and site.session_link_enabled,
        slash_commands=repo.slash_commands.enabled,
    )


def resolve_consolidation_chain(*, site: SiteSnapshot, repo: RepositoryConfig) -> ModelChain:
    """Row 9. Unlike ``resolve_agent_settings`` it never raises, so a site consolidation model still consolidates
    when no agent model is set anywhere."""
    repo_model, repo_fallback = _repo_agent_chain(site, repo.models.agent)
    return ModelChain(names=(site.memory_consolidation_model_name or repo_model, repo_fallback))


def resolve_pipeline_watch_enabled(*, site: SiteSnapshot, repo: RepositoryConfig) -> bool:
    """Row 12's switch."""
    return repo.pipeline_watch.enabled and site.pipeline_watch_enabled


def resolve_pipeline_watch_max_attempts(*, site: SiteSnapshot, repo: RepositoryConfig) -> int:
    """Row 12's attempt cap."""
    site_cap = site.pipeline_watch_max_attempts
    return min(_repo_value(repo.pipeline_watch, "max_attempts", site_cap), site_cap)


def _resolve_agent(*, site: SiteSnapshot, repo_agent: AgentModelConfig, run: RunOverrides) -> ModelChain:
    repo_chain = _repo_agent_chain(site, repo_agent)
    names: tuple[str, ...]
    if run.model_names:
        names, thinking_level = run.model_names, _run_thinking_level(run)
    elif run.agent_model:
        names = (run.agent_model, *repo_chain)
        thinking_level = _run_thinking_level(run) or _inherited_thinking_level(site, repo_agent)
    elif run.use_max:
        names = (site.agent_max_model_name, *repo_chain)
        thinking_level = _site_thinking_level(site, "agent_max_thinking_level")
    elif repo_chain[0]:
        names = repo_chain
        thinking_level = _run_thinking_level(run) or _inherited_thinking_level(site, repo_agent)
    else:
        raise _no_agent_model_error()
    chain = ModelChain(
        names=names,
        thinking_level=thinking_level,
        fallback_thinking_level=_site_thinking_level(site, "agent_fallback_thinking_level"),
    )
    if not chain.names:
        raise _no_agent_model_error()
    return chain


def _no_agent_model_error() -> AgentConfigurationError:
    # Imported here: validators pulls in the agent stack, which the memory tasks and the watch policy must not load.
    from .validators import AgentConfigurationError

    return AgentConfigurationError(
        "No agent model configured. Set the system default (DAIV_AGENT_MODEL_NAME / "
        "site settings), set `models.agent.model` in the repository's .daiv.yml, or pass an explicit "
        "`agent_model` override."
    )


def _run_thinking_level(run: RunOverrides) -> ThinkingLevel | None:
    return _thinking_level(run.agent_thinking_level, source="the run")


def _inherited_thinking_level(site: SiteSnapshot, repo_agent: AgentModelConfig) -> ThinkingLevel | None:
    if "thinking_level" in repo_agent.model_fields_set:
        return repo_agent.thinking_level
    return _site_thinking_level(site, "agent_thinking_level")


def _site_thinking_level(site: SiteSnapshot, field: str) -> ThinkingLevel | None:
    return _thinking_level(getattr(site, field), source=f"site {field}")


def _thinking_level(raw: str | None, *, source: str) -> ThinkingLevel | None:
    if not raw:
        return None
    try:
        return ThinkingLevel(raw)
    except ValueError:
        logger.error("Invalid thinking level %r from %s; running without thinking.", raw, source)
        return None


def _repo_agent_chain(site: SiteSnapshot, repo_agent: AgentModelConfig) -> tuple[str, str]:
    return _repo_chain(repo_agent, site_model=site.agent_model_name, site_fallback=site.agent_fallback_model_name)


def _repo_chain(section: BaseModel, *, site_model: str, site_fallback: str) -> tuple[str, str]:
    return _repo_value(section, "model", site_model), _repo_value(section, "fallback_model", site_fallback)


def _repo_value[T](section: BaseModel, field: str, site_value: T) -> T:
    if field in section.model_fields_set and (value := getattr(section, field)) not in (None, ""):
        return value
    return site_value


def _run_or_site[T](run_value: T | None, site_value: T) -> T:
    return site_value if run_value is None else run_value
