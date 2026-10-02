"""Rows 1 and 2 of the config-resolver plan as data: the model chain and thinking level one agent run gets.

A repo's ``models.agent`` holds only the keys it sets: ``{"thinking_level": None}`` disables thinking, ``{}`` leaves it
to the site. A case whose run carries ``model_names`` is the exact chain, which the executor decides before it resolves
anything. Ids ending ``-d1`` pin divergence D1; ``-through-raw`` marks a site thinking level that reaches the model
unvalidated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SITE: dict[str, Any] = {
    "agent_model_name": "site-model",
    "agent_fallback_model_name": "site-fallback",
    "agent_thinking_level": "medium",
    "agent_max_model_name": "site-max-model",
    "agent_max_thinking_level": "high",
}

REPO: dict[str, Any] = {"model": "repo-model", "fallback_model": "repo-fallback", "thinking_level": "low"}

RUN_MODEL = "run-model"


@dataclass(frozen=True)
class AgentSettingsCase:
    id: str
    site: dict[str, Any]
    repo_agent: dict[str, Any]
    run: dict[str, Any]
    chain: tuple[str, ...] = ()
    thinking_level: str | None = None
    raises: bool = False

    @property
    def exact_chain(self) -> bool:
        return bool(self.run.get("model_names"))


def _case(
    id: str,  # noqa: A002
    *,
    site: dict[str, Any] | None = None,
    repo: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
    chain: tuple[str, ...] = (),
    thinking: str | None = None,
    raises: bool = False,
) -> AgentSettingsCase:
    return AgentSettingsCase(
        id=id,
        site=SITE | (site or {}),
        repo_agent=repo or {},
        run=run or {},
        chain=chain,
        thinking_level=thinking,
        raises=raises,
    )


AGENT_SETTINGS_CASES: tuple[AgentSettingsCase, ...] = (
    # Exact chain: as given, nothing appended, and no thinking is not the site default.
    _case(
        "exact-chain-runs-as-given",
        repo=REPO,
        run={"model_names": ("exact-a", "exact-b"), "agent_thinking_level": "high"},
        chain=("exact-a", "exact-b"),
        thinking="high",
    ),
    _case(
        "exact-chain-without-thinking-disables-it",
        repo=REPO,
        run={"model_names": ("exact-a", "exact-b"), "agent_thinking_level": None},
        chain=("exact-a", "exact-b"),
        thinking=None,
    ),
    # Run override: the override first, then the repo's model and fallback; thinking from the run, then the repo.
    _case(
        "override-leads-the-repo-chain",
        repo=REPO,
        run={"agent_model": RUN_MODEL},
        chain=(RUN_MODEL, "repo-model", "repo-fallback"),
        thinking="low",
    ),
    _case(
        "override-run-thinking-beats-the-repos",
        repo=REPO,
        run={"agent_model": RUN_MODEL, "agent_thinking_level": "high"},
        chain=(RUN_MODEL, "repo-model", "repo-fallback"),
        thinking="high",
    ),
    _case(
        "override-repo-null-thinking-disables-thinking",
        repo=REPO | {"thinking_level": None},
        run={"agent_model": RUN_MODEL},
        chain=(RUN_MODEL, "repo-model", "repo-fallback"),
        thinking=None,
    ),
    _case(
        "override-run-thinking-beats-a-repo-null",
        repo=REPO | {"thinking_level": None},
        run={"agent_model": RUN_MODEL, "agent_thinking_level": "xhigh"},
        chain=(RUN_MODEL, "repo-model", "repo-fallback"),
        thinking="xhigh",
    ),
    _case(
        "override-with-an-unset-repo-falls-back-to-the-site",
        repo={},
        run={"agent_model": RUN_MODEL},
        chain=(RUN_MODEL, "site-model", "site-fallback"),
        thinking="medium",
    ),
    _case(
        "override-repo-sets-only-the-model",
        repo={"model": "repo-model"},
        run={"agent_model": RUN_MODEL},
        chain=(RUN_MODEL, "repo-model", "site-fallback"),
        thinking="medium",
    ),
    _case(
        "override-beats-max",
        repo=REPO,
        run={"agent_model": RUN_MODEL, "use_max": True},
        chain=(RUN_MODEL, "repo-model", "repo-fallback"),
        thinking="low",
    ),
    _case(
        "override-gets-no-thinking-when-the-site-level-is-invalid",
        site={"agent_thinking_level": "bogus"},
        repo={},
        run={"agent_model": RUN_MODEL},
        chain=(RUN_MODEL, "site-model", "site-fallback"),
        thinking=None,
    ),
    _case(
        "override-run-thinking-beats-an-invalid-site-level",
        site={"agent_thinking_level": "bogus"},
        repo={},
        run={"agent_model": RUN_MODEL, "agent_thinking_level": "low"},
        chain=(RUN_MODEL, "site-model", "site-fallback"),
        thinking="low",
    ),
    _case(
        "override-needs-no-site-default",
        site={"agent_model_name": ""},
        repo=REPO,
        run={"agent_model": RUN_MODEL},
        chain=(RUN_MODEL, "repo-model", "repo-fallback"),
        thinking="low",
    ),
    # Max (the daiv-max label): the site's max model first, then the repo's; thinking from the site's max level only.
    _case(
        "max-leads-the-repo-chain",
        repo=REPO,
        run={"use_max": True},
        chain=("site-max-model", "repo-model", "repo-fallback"),
        thinking="high",
    ),
    _case(
        "max-ignores-the-run-thinking-level",
        repo=REPO,
        run={"use_max": True, "agent_thinking_level": "minimal"},
        chain=("site-max-model", "repo-model", "repo-fallback"),
        thinking="high",
    ),
    _case(
        "max-with-an-unset-repo-falls-back-to-the-site",
        repo={},
        run={"use_max": True},
        chain=("site-max-model", "site-model", "site-fallback"),
        thinking="high",
    ),
    _case(
        "max-passes-an-invalid-site-max-level-through-raw",
        site={"agent_max_thinking_level": "bogus"},
        repo=REPO,
        run={"use_max": True},
        chain=("site-max-model", "repo-model", "repo-fallback"),
        thinking="bogus",
    ),
    _case(
        "max-needs-no-site-default",
        site={"agent_model_name": ""},
        repo=REPO,
        run={"use_max": True},
        chain=("site-max-model", "repo-model", "repo-fallback"),
        thinking="high",
    ),
    # Default: the site's model and fallback, whatever the repo says; thinking from the run, then the site.
    _case("default-uses-the-site", repo={}, run={}, chain=("site-model", "site-fallback"), thinking="medium"),
    _case(
        "default-ignores-the-repo-model-and-thinking-d1",
        repo=REPO,
        run={},
        chain=("site-model", "site-fallback"),
        thinking="medium",
    ),
    _case(
        "default-ignores-a-repo-null-thinking-d1",
        repo={"thinking_level": None},
        run={},
        chain=("site-model", "site-fallback"),
        thinking="medium",
    ),
    _case(
        "default-run-thinking-beats-the-sites",
        repo=REPO,
        run={"agent_thinking_level": "low"},
        chain=("site-model", "site-fallback"),
        thinking="low",
    ),
    _case(
        "default-passes-an-invalid-site-level-through-raw",
        site={"agent_thinking_level": "bogus"},
        repo={},
        run={},
        chain=("site-model", "site-fallback"),
        thinking="bogus",
    ),
    _case(
        "default-run-thinking-beats-an-invalid-site-level",
        site={"agent_thinking_level": "bogus"},
        repo={},
        run={"agent_thinking_level": "low"},
        chain=("site-model", "site-fallback"),
        thinking="low",
    ),
    _case("default-without-a-site-model-raises", site={"agent_model_name": ""}, repo=REPO, run={}, raises=True),
    _case(
        "default-without-a-site-model-raises-with-run-thinking",
        site={"agent_model_name": ""},
        repo=REPO,
        run={"agent_thinking_level": "low"},
        raises=True,
    ),
)
