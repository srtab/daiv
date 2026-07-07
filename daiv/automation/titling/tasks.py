from __future__ import annotations

import logging
import re
from typing import Literal, cast

from django_tasks import task
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from automation.agent.base import BaseAgent
from automation.titling.services import MAX_TITLE_LENGTH
from core.site_settings import site_settings

logger = logging.getLogger("daiv.automation.titling")

_SYSTEM_PROMPT = (
    "Generate a concise 3-6 word title for the following coding task.\n"
    "Plain text only — no quotes, markdown, or trailing punctuation."
)

_GENERIC_REFS = frozenset({"main", "master", "dev", "develop", "trunk", "staging", "production", "prod"})
_SHA_LIKE = re.compile(r"^[0-9a-fA-F]{7,40}$")


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
    """Build the structured LLM chain with fallback. Raises ``RuntimeError`` if no model is configured."""

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


@task()
def generate_title_task(
    entity_type: Literal["session", "run", "chat_thread", "activity"], pk: str, prompt: str, repo_id: str, ref: str = ""
) -> None:
    """Overwrite a Session/Run (or legacy ChatThread/Activity) title with an LLM-generated one.

    Failures propagate to django-tasks (which logs + marks the task failed); the
    title set synchronously remains (heuristic for chat sessions, possibly empty
    for prompt-driven runs). The legacy ``chat_thread``/``activity`` literals stay
    supported during the sessions-unification dual period (removed in Task 15).
    """
    if entity_type == "session":
        from sessions.models import Session

        model_cls = Session
    elif entity_type == "run":
        from sessions.models import Run

        model_cls = Run
    elif entity_type == "chat_thread":
        from chat.models import ChatThread

        model_cls = ChatThread
    else:
        from activity.models import Activity

        model_cls = Activity

    try:
        entity = model_cls.objects.get(pk=pk)
    except model_cls.DoesNotExist:
        logger.warning("generate_title_task: %s with pk=%s not found, skipping", entity_type, pk)
        return

    try:
        structured_llm = _build_structured_llm()
    except RuntimeError:
        logger.exception(
            "generate_title_task: model not configured for %s pk=%s — feature disabled until API key is set",
            entity_type,
            pk,
        )
        return

    title = _invoke_titler(
        structured_llm,
        prompt=prompt,
        repo_id=repo_id,
        ref=ref,
        run_metadata={"entity_type": entity_type, "entity_pk": pk, "repo_id": repo_id, "ref": ref},
    )

    entity.title = title
    entity.save(update_fields=["title"])


@task()
def generate_batch_title_task(batch_id: str, prompt: str) -> None:
    """Generate a single LLM title for a Run batch and apply it to every untitled member.

    One LLM call per batch instead of one per run. Repo/ref context is omitted because batch
    members typically span repos. Only rows with an empty ``title`` are updated, so synchronous
    titles (e.g. scheduled-run templates) are preserved. The same title is stamped on each
    affected run's Session when the session title is still empty.
    """
    from sessions.models import Run, Session

    try:
        structured_llm = _build_structured_llm()
    except RuntimeError:
        logger.exception(
            "generate_batch_title_task: model not configured for batch_id=%s — feature disabled until API key is set",
            batch_id,
        )
        return

    title = _invoke_titler(
        structured_llm, prompt=prompt, run_metadata={"entity_type": "run_batch", "batch_id": batch_id}
    )

    updated = Run.objects.filter(batch_id=batch_id, title="").update(title=title)
    # Backfill the parent session title too — a fresh batch creates one session per run,
    # each with an empty title until this titler lands.
    Session.objects.filter(runs__batch_id=batch_id, title="").update(title=title)
    if updated == 0:
        logger.warning(
            "generate_batch_title_task: no rows updated for batch_id=%s (stale batch or all already titled)", batch_id
        )
    else:
        logger.info("generate_batch_title_task: updated %d runs for batch_id=%s", updated, batch_id)
