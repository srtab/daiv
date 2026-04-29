from __future__ import annotations

import logging
import re
from typing import Literal, cast

from django_tasks import task
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from automation.agent.base import BaseAgent
from automation.agent.constants import ModelName
from automation.titling.services import MAX_TITLE_LENGTH

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
        pattern=r'^[^\s"\'#*].+',
        description="3-6 words. Plain text only — no quotes, markdown, or trailing punctuation.",
    )


@task()
def generate_title_task(
    entity_type: Literal["chat_thread", "activity"], pk: str, prompt: str, repo_id: str, ref: str = ""
) -> None:
    """Overwrite a ChatThread/Activity title with an LLM-generated one.

    Failures propagate to django-tasks (which logs + marks the task failed); the
    title set synchronously remains (heuristic for chat threads, possibly empty
    for prompt-driven activities).
    """
    if entity_type == "chat_thread":
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

    def _structured(model_name: str):
        return (
            BaseAgent
            .get_model(model=model_name, max_tokens=60)
            .with_structured_output(GeneratedTitle)
            .with_retry(stop_after_attempt=2)
        )

    try:
        structured_llm = _structured(ModelName.GPT_5_4_MINI).with_fallbacks([_structured(ModelName.CLAUDE_HAIKU_4_5)])
    except RuntimeError:
        logger.exception(
            "generate_title_task: model not configured for %s pk=%s — feature disabled until API key is set",
            entity_type,
            pk,
        )
        return

    ref = ref.strip()
    user_text = f"Repository: {repo_id}\n"
    if _ref_is_informative(ref):
        user_text += f"Branch: {ref}\n"
    user_text += f"Task: {prompt[:500]}"

    run_name = "Titling"
    result = cast(
        "GeneratedTitle",
        structured_llm.with_config(
            run_name=run_name,
            tags=[run_name, f"entity:{entity_type}"],
            metadata={"entity_type": entity_type, "entity_pk": pk, "repo_id": repo_id, "ref": ref},
        ).invoke([SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=user_text)]),
    )

    entity.title = result.title.strip()
    entity.save(update_fields=["title"])
