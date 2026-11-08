"""Session and run titles: :func:`heuristic_title` at creation, then an LLM title that the title tasks in
``sessions.tasks`` generate in the background."""

from __future__ import annotations

import re
from typing import cast

from pydantic import BaseModel, Field

from core.site_settings import site_settings

MAX_TITLE_LENGTH = 120
MAX_HEURISTIC_LENGTH = 80

_FILLER_PREFIXES = re.compile(
    r"^(?:can you|could you|please|i need|i want|help me|i'd like|i would like)\s+", re.IGNORECASE
)

_SYSTEM_PROMPT = (
    "You title sessions in DAIV, an AI coding agent. People find a session again by scanning a list of titles or "
    "searching them, so the title must say what this task is and tell it apart from every other session.\n\n"
    "- Start with the action and its subject, using the request's key terms. A question becomes what it asks "
    "about.\n"
    "- Keep what a person would search for, copied exactly: ticket, issue and merge request references, error "
    "and Sentry issue IDs, versions, and names from the code such as files, classes, functions and settings. "
    'Put identifiers after the subject: "Fix 503 from the payments API (SHOP-API-1F2)".\n'
    "- Prefer the name a person would remember over a raw value such as an IP address, URL, or hash.\n"
    "- The repository and branch are shown next to the title, so do not repeat them.\n"
    "- Leave out boilerplate around the request and words that fit any task, such as task, request, handling, "
    "processing, or review.\n"
    '- A slash command such as "/qa-run-suite 4521 mode=dry-run" names the task: write its name as words, '
    'keeping every part, then its arguments: "QA run suite 4521 (dry run)". When text follows the command, '
    "title what the text asks. Do not guess what a referenced item is about.\n"
    "- A message with no task in it, like a greeting or a test, is titled for what it is, such as "
    '"Greeting" or "Test message". Never invent a task.\n\n'
    "2 to 12 words, the most telling ones first, since the list cuts long titles off. Write in the request's "
    "language. Sentence case with acronyms in capitals and identifiers in their own casing. Plain text: no "
    "quotes, markdown, or trailing punctuation."
)

_GENERIC_REFS = frozenset({"main", "master", "dev", "develop", "trunk", "staging", "production", "prod"})
_SHA_LIKE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def heuristic_title(prompt: str) -> str:
    """Best-effort title from ``prompt`` without calling an LLM: first non-empty
    line, leading filler prefix stripped, truncated to ``MAX_HEURISTIC_LENGTH``.
    """
    first_line = next((stripped for line in prompt.splitlines() if (stripped := line.strip())), "")
    return _FILLER_PREFIXES.sub("", first_line).strip()[:MAX_HEURISTIC_LENGTH]


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
        description="2-12 words, sentence case, plain text: no quotes, markdown, or trailing punctuation.",
    )


def _build_structured_llm():
    """Build the structured LLM chain with fallback. Raises ``RuntimeError`` if no model is configured.

    One of three deliberately separate copies — ``notifications.classification.llm`` and ``memory.llm`` have
    their own, with divergent retry policies; read those before reconciling them.
    """
    from automation.agent.base import get_model

    def _structured(model_name: str):
        # No ``max_tokens`` cap: reasoning models (GPT-5, Claude thinking) count reasoning
        # tokens toward the budget, so a tight cap starves the structured-output JSON and
        # raises LengthFinishReasonError. Title length is bounded by ``GeneratedTitle.title``.
        return get_model(model=model_name).with_structured_output(GeneratedTitle).with_retry(stop_after_attempt=2)

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
    user_text += f"Request: {prompt[:500]}"

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
    """A title of up to 12 words for ``prompt``, its trace tagged with ``run_metadata``.

    Raises :class:`TitlerNotConfiguredError` when no titling model can be built; an error from the model call itself
    propagates unchanged.
    """
    try:
        structured_llm = _build_structured_llm()
    except RuntimeError as exc:
        raise TitlerNotConfiguredError(str(exc)) from exc
    return _invoke_titler(structured_llm, prompt=prompt, repo_id=repo_id, ref=ref, run_metadata=run_metadata)
