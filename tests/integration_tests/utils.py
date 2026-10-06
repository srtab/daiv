import json
import os
import subprocess  # noqa: S404
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from langchain.messages import AIMessage

from automation.agent.agent_settings import RunOverrides, resolve_agent_settings
from automation.agent.base import _BARE_NAME_HEURISTICS
from automation.agent.usage_tracking import build_usage_summary, track_usage_metadata
from core.constants import ModelName
from core.site_settings import site_settings

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from langchain_core.messages import BaseMessage
    from langchain_core.tools import ToolCall

    from automation.agent.agent_settings import AgentSettings
    from codebase.context import RuntimeCtx

INTERRUPT_ALL_TOOLS_CONFIG = {
    # SkillMiddleware
    "skill": True,
    # TodoListMiddleware
    "write_todos": True,
    # FilesystemMiddleware
    "grep": True,
    "glob": True,
    "ls": True,
    "read_file": True,
    "edit_file": True,
    "write_file": True,
    # SubAgentMiddleware
    "task": True,
    # SandboxMiddleware
    "bash": True,
    # WebFetchMiddleware
    "web_fetch": True,
    # WebSearchMiddleware
    "web_search": True,
    # GitPlatformMiddleware
    "github": True,
    "gitlab": True,
}

CODING_MODEL_NAMES = [
    ModelName.CLAUDE_SONNET_4_5,
    ModelName.CLAUDE_SONNET_4_6,
    ModelName.CLAUDE_OPUS_4_5,
    ModelName.CLAUDE_OPUS_4_6,
    ModelName.GPT_5_3_CODEX,
    ModelName.GPT_5_4,
    ModelName.Z_AI_GLM_5_1,
    ModelName.MINIMAX_M3,
    ModelName.MOONSHOTAI_KIMI_K2_6,
]

# What production runs for this task (`diff_to_metadata_model_name` and its fallback). The suite
# defaults to these two: at 12 cases each, the full candidate list is 84 paid runs and ~20 minutes.
_PRODUCTION_MODELS = [ModelName.GEMINI_3_7_FLASH, ModelName.DEEPSEEK_V4_FLASH_0731]

_CANDIDATE_MODELS = [
    ModelName.GPT_5_4_MINI,
    ModelName.CLAUDE_HAIKU_4_5,
    ModelName.GPT_5_6_LUNA,
    ModelName.Z_AI_GLM_5_3_FLASH,
    ModelName.MOONSHOTAI_KIMI_K2_7_CODE,
]

# Set DAIV_EVAL_ALL_MODELS=1 to score the candidates too, e.g. before changing the site default.
FAST_MODEL_NAMES = (
    _PRODUCTION_MODELS + _CANDIDATE_MODELS if os.environ.get("DAIV_EVAL_ALL_MODELS") else _PRODUCTION_MODELS
)

_PROVIDER_ENV_VAR = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "google": "GOOGLE_API_KEY",  # alias parse_model_spec accepts
    "openrouter": "OPENROUTER_API_KEY",
}


def _resolve_provider_slug(model_spec: str) -> str:
    if ":" in model_spec:
        return model_spec.split(":", 1)[0]
    for prefixes, slug in _BARE_NAME_HEURISTICS:
        if model_spec.startswith(prefixes):
            return slug
    return model_spec


def require_provider_for_model(model_spec: str) -> None:
    """Skip the current test if the provider for ``model_spec`` has no API key.

    Built-in providers map to the canonical env vars (OPENROUTER_API_KEY, etc.).
    Custom providers use the DAIV_TEST_PROVIDER_<SLUG>_API_KEY convention from
    conftest._provision_providers; both must be set for the row to exist.
    """
    slug = _resolve_provider_slug(model_spec)
    env_var = _PROVIDER_ENV_VAR.get(slug)
    if env_var is None:
        env_var = f"DAIV_TEST_PROVIDER_{slug.upper()}_API_KEY"
    if not os.environ.get(env_var):
        pytest.skip(f"{env_var} not set; cannot run against {model_spec!r}.")


def agent_settings_on(model_name: str, ctx: RuntimeCtx) -> AgentSettings:
    """A run's settings with ``model_name`` alone as its agent chain, at the site's thinking level."""
    site = site_settings.snapshot()
    run = RunOverrides(model_names=(model_name,), agent_thinking_level=site.agent_thinking_level)
    return resolve_agent_settings(site=site, repo=ctx.config, run=run)


def extract_tool_calls(messages: list[BaseMessage]) -> list[ToolCall]:
    return [tool_call for message in messages if isinstance(message, AIMessage) for tool_call in message.tool_calls]


def _models_from_env(env_var: str, default: Sequence[str]) -> list[str]:
    """``default``, or a comma-separated model-spec override from ``env_var``.

    Any spec ``parse_model_spec`` accepts, not only a ``ModelName``. DAIV_EVAL_ALL_MODELS
    deliberately does not widen the suites parametrized on these lists.
    """
    override = [spec.strip() for spec in os.environ.get(env_var, "").split(",") if spec.strip()]
    return override or list(default)


MEMORY_EXTRACTION_MODELS = _models_from_env(
    "DAIV_EVAL_MEMORY_EXTRACTION_MODELS", [ModelName.GPT_5_4_MINI, ModelName.CLAUDE_HAIKU_4_5]
)
MEMORY_CONSOLIDATION_MODELS = _models_from_env(
    "DAIV_EVAL_MEMORY_CONSOLIDATION_MODELS", [ModelName.CLAUDE_SONNET_4_6, ModelName.GPT_5_3_CODEX]
)

# In neither matrix above: GPT_5_3_CODEX is a graded consolidation cell and would grade its own
# output. Same vendor as two graded cells, which is acceptable only because the primary gate —
# the decision check — is deterministic and never calls the judge.
MEMORY_JUDGE_MODEL = ModelName.CLAUDE_OPUS_4_6

# Keep in step with EVAL_MODEL in the Makefile.
EVAL_MODEL = "openrouter:z-ai/glm-5.2"

ASK_USER_MODELS = _models_from_env("DAIV_EVAL_ASK_USER_MODELS", CODING_MODEL_NAMES)
SKILLS_MODELS = _models_from_env("DAIV_EVAL_SKILLS_MODELS", CODING_MODEL_NAMES)
TODOS_MODELS = _models_from_env("DAIV_EVAL_TODOS_MODELS", [EVAL_MODEL])
WEB_SEARCH_MODELS = _models_from_env("DAIV_EVAL_WEB_SEARCH_MODELS", [EVAL_MODEL])
SUBAGENTS_MODELS = _models_from_env("DAIV_EVAL_SUBAGENTS_MODELS", [EVAL_MODEL])

# A case's result is the majority of its repetitions. 1 is for local iteration and is not a gate.
EVAL_REPEATS = int(os.environ.get("DAIV_EVAL_REPEATS", "3"))


def final_text(messages: Sequence[BaseMessage]) -> str:
    return messages[-1].text if messages and isinstance(messages[-1], AIMessage) else ""


async def run_agent_once(
    model_name: str, prompt: str, *, interrupt_on: dict[str, bool] | None = None, ask_user_enabled: bool = True
) -> dict:
    """One fresh agent run on a disk clone of srtab/daiv ``main``; returns the run's final state."""
    from langgraph.checkpoint.memory import InMemorySaver
    from sandbox_envs.services import build_sandbox_spec

    from automation.agent.graph import create_daiv_agent
    from automation.agent.workspace.disk import DiskWorkspace
    from codebase.base import Scope
    from codebase.context import set_runtime_ctx

    async with set_runtime_ctx(
        repo_id="srtab/daiv", scope=Scope.GLOBAL, ref="main", sandbox_spec=await build_sandbox_spec(None)
    ) as ctx:
        agent = await create_daiv_agent(
            settings=agent_settings_on(model_name, ctx),
            ctx=ctx,
            auto_commit_changes=False,
            checkpointer=InMemorySaver(),
            interrupt_on=interrupt_on,
            workspace=DiskWorkspace(ctx),
            ask_user_enabled=ask_user_enabled,
        )
        return await agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            context=ctx,
            config={"configurable": {"thread_id": "1"}},
        )


@dataclass
class RunMetrics:
    """One measured agent call. ``measure`` fills ``usage``; the test sets ``messages`` and any suite ``extra``."""

    messages: Sequence[BaseMessage] = ()
    extra: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {**self.usage, "turns": sum(isinstance(message, AIMessage) for message in self.messages), **self.extra}


@contextmanager
def measure(request: pytest.FixtureRequest) -> Generator[RunMetrics]:
    """Record the enclosed agent call's tokens and cost on ``request.node.eval_metrics``.

    Usage is recorded even when the block raises, so a failing case still reports what it spent. Keep judge calls
    outside the block: everything the block calls is counted.
    """
    metrics = RunMetrics()
    request.node.eval_metrics = metrics
    with track_usage_metadata() as handler:
        try:
            yield metrics
        finally:
            summary = build_usage_summary(handler)
            metrics.usage = {
                "input_tokens": summary.input_tokens,
                "output_tokens": summary.output_tokens,
                "cache_read_tokens": sum(
                    (usage.get("input_token_details") or {}).get("cache_read", 0)
                    for usage in handler.usage_metadata.values()
                ),
                "cost": float(summary.cost_usd) if summary.cost_usd is not None else None,
            }


@cache
def _git_sha() -> str:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607

    sha = git("rev-parse", "HEAD").stdout.strip() or "unknown"
    return sha if git("diff", "--quiet", "HEAD").returncode == 0 else f"{sha}+dirty"


def eval_metrics_row(item: pytest.Item, *, passed: bool | None, run: int, git_sha: str) -> dict[str, Any]:
    """One ``evals/compare_runs.py`` row: the test's identity and outcome plus what ``measure`` recorded."""
    callspec = getattr(item, "callspec", None)
    marker = item.get_closest_marker("langsmith")
    metrics: RunMetrics | None = getattr(item, "eval_metrics", None)
    return {
        "nodeid": item.nodeid,
        "suite": (marker.kwargs.get("test_suite_name") if marker else None) or item.module.__name__,
        "case": item.name,
        "model": callspec.params.get("model_name") if callspec else None,
        "run": run,
        "git_sha": git_sha,
        "passed": passed,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "turns": None,
        "cost": None,
        **(metrics.as_row() if metrics else {}),
    }


def write_eval_metrics_row(item: pytest.Item, report: pytest.TestReport) -> None:
    """Append an eval case's row for this pass to ``$DAIV_EVAL_METRICS_OUT``; a no-op when the variable is unset.

    An eval case is a test taking ``eval_request``; other tests write nothing. Every eval case writes one row per pass,
    with ``passed: null`` when it cast no vote:
    - it skipped, or its setup failed;
    - it never entered ``measure``, so made no measured agent call;
    - it failed before the agent spent any tokens (a clone, GitLab or provider outage).
    """
    out = os.environ.get("DAIV_EVAL_METRICS_OUT")
    if not out or "eval_request" not in item.fixturenames:
        return
    if report.when == "teardown" or (report.when == "setup" and report.passed):
        return
    metrics: RunMetrics | None = getattr(item, "eval_metrics", None)
    voted = (
        report.when == "call"
        and not report.skipped
        and metrics is not None
        and (report.passed or bool(metrics.usage.get("input_tokens")))
    )
    row = eval_metrics_row(
        item, passed=report.passed if voted else None, run=int(os.environ.get("DAIV_EVAL_RUN", "1")), git_sha=_git_sha()
    )
    with Path(out).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
