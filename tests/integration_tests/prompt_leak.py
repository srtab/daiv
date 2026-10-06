"""Collection-time guard against eval vocabulary in the agent's prompts.

A prompt change must not pass its eval by copying a case's wording, so every suite with eval cases runs
``assert_no_prompt_leak`` over its case texts at collection, with two rules:

- no case text may share an 8-word span (``memory_grading.shared_span``) with any prompt the agent can be sent;
- a 3-7 word case text may not appear whole in any prompt outside ``skills/``, which quote trigger phrases by design.
"""

from __future__ import annotations

import importlib
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from .memory_grading import _LEAK_SPAN_WORDS, shared_span

if TYPE_CHECKING:
    from collections.abc import Iterable

_MIN_WHOLE_CASE_WORDS = 3

_PROMPT_MODULES = (
    "automation.agent.prompts",
    "automation.agent.graph",
    "automation.agent.middlewares.artifacts",
    "automation.agent.middlewares.ensure_response",
    "automation.agent.middlewares.file_system",
    "automation.agent.middlewares.git",
    "automation.agent.middlewares.git_platform",
    "automation.agent.middlewares.memory",
    "automation.agent.middlewares.sandbox",
    "automation.agent.middlewares.skills",
    "automation.agent.middlewares.step_budget",
    "automation.agent.middlewares.web_fetch",
    "automation.agent.middlewares.web_search",
    "automation.agent.subagents",
    "automation.agent.deferred.prompt",
    "automation.agent.deferred.search_tool",
    "automation.agent.workspace.sandbox_backend",
    "langchain.agents.middleware.todo",
)


def _template_text(value: object) -> str | None:
    template = getattr(getattr(value, "prompt", value), "template", None)
    return template if isinstance(template, str) else None


@cache
def agent_prompt_texts() -> dict[str, str]:
    """Every prompt text the agent can be sent, by name.

    Upper-case string and prompt-template constants of all agent prompt modules (core, middlewares, deferred),
    function-computed system prompts (explore, general-purpose, output-invariants), the harness profile's
    tool-description overrides, and every built-in skill and detector charter markdown file.
    """
    import automation.agent
    from automation.agent.constants import REPO_PATH
    from automation.agent.graph import _output_invariants_system_prompt
    from automation.agent.profile import DAIV_HARNESS_PROFILE
    from automation.agent.subagents import _explore_system_prompt, _general_purpose_system_prompt

    texts: dict[str, str] = {}
    for module_name in _PROMPT_MODULES:
        for attr, value in vars(importlib.import_module(module_name)).items():
            if attr.isupper() and (text := value if isinstance(value, str) else _template_text(value)):
                texts[f"{module_name}.{attr}"] = text
    texts["explore_system_prompt"] = _explore_system_prompt(f"{REPO_PATH}/")
    texts["general_purpose_system_prompt"] = _general_purpose_system_prompt(f"{REPO_PATH}/")
    texts["output_invariants_system_prompt"] = _output_invariants_system_prompt(f"{REPO_PATH}/")
    for tool_name, description in DAIV_HARNESS_PROFILE.tool_description_overrides.items():
        texts[f"tool_description_overrides.{tool_name}"] = description
    skills_root = Path(automation.agent.__file__).parent / "skills"
    for path in sorted(skills_root.rglob("*.md")):
        texts[f"skills/{path.relative_to(skills_root)}"] = path.read_text(encoding="utf-8")
    return texts


def assert_no_prompt_leak(case_texts: Iterable[str]) -> None:
    """Raise ``ValueError`` on either leak rule.

    - a case text shares an 8-word span with any prompt;
    - a 3-7 word case text appears whole in a prompt outside ``skills/``.
    """
    prompts = agent_prompt_texts()
    for text in case_texts:
        words = len(text.split())
        for name, prompt in prompts.items():
            span = shared_span(prompt, text)
            if not span and _MIN_WHOLE_CASE_WORDS <= words < _LEAK_SPAN_WORDS and not name.startswith("skills/"):
                span = shared_span(prompt, text, words=words)
            if span:
                raise ValueError(
                    f"An eval case shares the span {span!r} with {name}. A prompt must not copy eval case wording; "
                    "reword the prompt (or, in a case-fix PR, the case)."
                )
