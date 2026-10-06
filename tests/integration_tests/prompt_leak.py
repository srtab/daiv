"""Collection-time guard against eval vocabulary in the agent's prompts.

A prompt change must not pass its eval by copying a case's wording, so every suite with eval cases runs
``assert_no_prompt_leak`` over its case texts at collection: no case text may share an 8-word span
(``memory_grading.shared_span``) with any prompt the agent can be sent.
"""

from __future__ import annotations

import importlib
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from .memory_grading import shared_span

if TYPE_CHECKING:
    from collections.abc import Iterable

_PROMPT_MODULES = (
    "automation.agent.prompts",
    "automation.agent.middlewares.file_system",
    "automation.agent.middlewares.sandbox",
    "automation.agent.middlewares.skills",
    "automation.agent.middlewares.web_search",
    "automation.agent.subagents",
    "langchain.agents.middleware.todo",
)


def _template_text(value: object) -> str | None:
    template = getattr(getattr(value, "prompt", value), "template", None)
    return template if isinstance(template, str) else None


@cache
def agent_prompt_texts() -> dict[str, str]:
    """Every prompt text the agent can be sent, by name.

    Upper-case string and prompt-template constants of the prompt modules, the explore and general-purpose subagent
    prompts, the harness profile's tool-description overrides, and every built-in skill and detector charter file.
    """
    import automation.agent
    from automation.agent.constants import REPO_PATH
    from automation.agent.profile import DAIV_HARNESS_PROFILE
    from automation.agent.subagents import _explore_system_prompt, _general_purpose_system_prompt

    texts: dict[str, str] = {}
    for module_name in _PROMPT_MODULES:
        for attr, value in vars(importlib.import_module(module_name)).items():
            if attr.isupper() and (text := value if isinstance(value, str) else _template_text(value)):
                texts[f"{module_name}.{attr}"] = text
    texts["explore_system_prompt"] = _explore_system_prompt(f"{REPO_PATH}/")
    texts["general_purpose_system_prompt"] = _general_purpose_system_prompt(f"{REPO_PATH}/")
    for tool_name, description in DAIV_HARNESS_PROFILE.tool_description_overrides.items():
        texts[f"tool_description_overrides.{tool_name}"] = description
    skills_root = Path(automation.agent.__file__).parent / "skills"
    for path in sorted(skills_root.rglob("*.md")):
        texts[f"skills/{path.relative_to(skills_root)}"] = path.read_text(encoding="utf-8")
    return texts


def assert_no_prompt_leak(case_texts: Iterable[str]) -> None:
    """Raise ``ValueError`` when a case text shares an 8-word span with any agent prompt."""
    prompts = agent_prompt_texts()
    for text in case_texts:
        for name, prompt in prompts.items():
            if span := shared_span(prompt, text):
                raise ValueError(
                    f"An eval case shares the span {span!r} with {name}. A prompt must not copy eval case wording; "
                    "reword the prompt (or, in a case-fix PR, the case)."
                )
