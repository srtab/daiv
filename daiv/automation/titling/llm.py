"""LLM titles for sessions and runs; the title tasks in ``sessions.tasks`` generate them in the background."""

from __future__ import annotations

import re
from typing import cast

from pydantic import BaseModel, Field

from automation.titling.services import MAX_TITLE_LENGTH
from core.site_settings import site_settings

_SYSTEM_PROMPT = (
    "Generate a concise 3-6 word title for the following coding task.\n"
    "Plain text only — no quotes, markdown, or trailing punctuation."
)

_GENERIC_REFS = frozenset({"main", "master", "dev", "develop", "trunk", "staging", "production", "prod"})
_SHA_LIKE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class TitlerNotConfiguredError(RuntimeError):
    """No titling model could be built, e.g. its provider has no API key configured."""


def _ref_is_informative(ref: str) -> bool:
    if not ref or ref.lower() in _GENERIC_REFS:
        return False
    return _SHA_LIKE.fullmatch(ref) is None


class GeneratedTitle(BaseModel):
    title: str = Field(
        min_length=3,
        max_length=MAX_TITLE_LENGTH,
        description="3-6 words. Plain text only — no quotes, markdown, or trailing punctuation.",
    )


def _build_structured_llm():
    """Build the structured LLM chain with fallback. Raises ``RuntimeError`` if no model is configured.

    One of three deliberately separate copies — ``sessions.classification`` and ``memory.llm`` have
    their own, with divergent retry policies; read those before reconciling them.
    """
    from automation.agent.base import BaseAgent

    def _structured(model_name: str):
        # No ``max_tokens`` cap: reasoning models (GPT-5, Claude thinking) count reasoning
        # tokens toward the budget, so a tight cap starves the structured-output JSON and
        # raises LengthFinishReasonError. Title length is bounded by ``GeneratedTitle.title``.
        return (
            BaseAgent
            .get_model(model=model_name)
            .with_structured_output(GeneratedTitle)
            .with_retry(stop_after_attempt=2)
        )

    return _structured(site_settings.titling_model_name).with_fallbacks([
        _structured(site_settings.titling_fallback_model_name)
    ])


def _invoke_titler(structured_llm, *, prompt: str, repo_id: str = "", ref: str = "", run_metadata: dict) -> str:
    """Invoke the titler chain and return the cleaned title string.

    ``repo_id`` and ``ref`` are optional context: omitted for batches that span multiple repos.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    ref = ref.strip()
    user_text = ""
    if repo_id:
        user_text += f"Repository: {repo_id}\n"
    if _ref_is_informative(ref):
        user_text += f"Branch: {ref}\n"
    user_text += f"Task: {prompt[:500]}"

    run_name = "Titling"
    tags = [run_name]
    if entity_type := run_metadata.get("entity_type"):
        tags.append(f"entity:{entity_type}")
    result = cast(
        "GeneratedTitle",
        structured_llm.with_config(run_name=run_name, tags=tags, metadata=run_metadata).invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=user_text),
        ]),
    )
    return result.title.strip()


def generate_title(prompt: str, *, repo_id: str = "", ref: str = "", run_metadata: dict[str, str]) -> str:
    """A 3-6 word title for ``prompt``, its trace tagged with ``run_metadata``.

    Raises :class:`TitlerNotConfiguredError` when no titling model can be built; an error from the model call itself
    propagates unchanged.
    """
    try:
        structured_llm = _build_structured_llm()
    except RuntimeError as exc:
        raise TitlerNotConfiguredError(str(exc)) from exc
    return _invoke_titler(structured_llm, prompt=prompt, repo_id=repo_id, ref=ref, run_metadata=run_metadata)
