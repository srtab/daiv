from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from skills.constants import FRONTMATTER_RE

from .constants import REPO_PATH
from .events import ASSISTANT_MESSAGE_EVENT, assistant_message_payload

logger = logging.getLogger("daiv.agent")

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.messages import BaseMessage

    from codebase.context import RuntimeCtx


def extract_text_content(content: str | list) -> str:
    """
    Extract text content from a message's content field.

    LangChain messages can have content as either a string or a list of content blocks.
    This function handles both formats and extracts the text.

    Args:
        content: The message content (string or list of content blocks)

    Returns:
        str: The extracted text content
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        # For list-based content (multimodal messages), extract text from text blocks
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                # Typed rather than key-present: providers send `text: null` on some blocks,
                # and joining a None raises instead of contributing an empty string.
                if isinstance(text := block.get("text"), str):
                    text_parts.append(text)
            elif isinstance(block, str):
                text_parts.append(block)
        return "".join(text_parts)

    # Fallback for unexpected types
    return str(content)


FINAL_ASSISTANT_TEXT_MAX_CHARS = 6000

# LoopBreakerMiddleware's terminal="error" reply, addressed to a parent agent rather than a human.
_PARENT_AGENT_PREFIX = "ERROR:"


def final_assistant_text(messages: Sequence[BaseMessage]) -> str | None:
    """The agent's closing summary for a human, or ``None`` when it wrote none.

    Walks back to the last ``AIMessage`` carrying prose: a turn can end on tool calls alone, and a
    trailing ``HumanMessage`` (a manager appending the user's follow-up) is never the agent's own
    summary. Truncated because callers feed it to a model alongside a diff.
    """
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        text = extract_text_content(message.content).strip()
        if not text or text.startswith(_PARENT_AGENT_PREFIX):
            continue
        if len(text) > FINAL_ASSISTANT_TEXT_MAX_CHARS:
            return text[:FINAL_ASSISTANT_TEXT_MAX_CHARS].rstrip() + "…[truncated]"
        return text
    return None


_WORKSPACE_PATH_PREFIX = re.compile(rf"(?<![\w.~/-]){re.escape(REPO_PATH)}/(?=[\w-]|\.[\w./-])")


def repo_relative_text(text: str) -> str:
    """``text`` with the workspace prefix removed from every repository path, for text DAIV posts to the platform.

    Only a prefix that starts a path is removed; one right after a word or path character, as in a URL's path or a
    longer path, is kept. The root itself, with or without a trailing slash, is kept since it has no repo-relative form.
    """
    return _WORKSPACE_PATH_PREFIX.sub("", text)


def build_langsmith_config(
    ctx: RuntimeCtx,
    *,
    trigger: str,
    model: str,
    thinking_level: str | None = None,
    agent_name: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
    extra_tags: list[str] | None = None,
    configurable: dict[str, Any] | None = None,
) -> RunnableConfig:
    """
    Build a standardized RunnableConfig with LangSmith metadata and tags.

    Ensures all agent invocations carry the dashboard-critical metadata fields
    (scope, repository, git_platform, trigger, model) regardless of the callsite.

    Args:
        ctx: The runtime context.
        trigger: What triggered this invocation (e.g. "job", "chat", "label", "mention", "diff_to_metadata").
        model: The primary model name used for this invocation.
        thinking_level: The thinking level, if applicable.
        agent_name: The agent name (e.g. "DAIV Agent"); recorded as the ``lc_agent_name`` metadata
            field (overriding the value bound at graph-build time) and also added as a run tag.
        extra_metadata: Additional metadata fields specific to the callsite.
        extra_tags: Additional tags specific to the callsite.
        configurable: Configurable dict (e.g. thread_id for checkpointed agents).

    Returns:
        A RunnableConfig with standardized metadata and tags.
    """
    metadata: dict[str, Any] = {
        "repository": ctx.repository.slug,
        "git_platform": ctx.git_platform.value,
        "trigger": trigger,
        "model": model,
    }
    if ctx.scope is not None:
        metadata["scope"] = ctx.scope
    if thinking_level is not None:
        metadata["thinking_level"] = thinking_level
    if agent_name:
        # Invocation-time ``metadata`` replaces the agent's bound ``lc_agent_name`` (set by
        # create_deep_agent) rather than merging, so without mirroring it here the tool-call
        # logger reads ``<unknown-agent>``.
        metadata["lc_agent_name"] = agent_name
    if extra_metadata:
        metadata.update(extra_metadata)

    tags: list[str] = [ctx.git_platform.value, ctx.repository.slug]
    if ctx.scope:
        tags.append(ctx.scope)
    if agent_name:
        tags.append(agent_name)
    if extra_tags:
        tags.extend(extra_tags)

    config_kwargs: dict[str, Any] = {"metadata": metadata, "tags": tags}
    if configurable is not None:
        config_kwargs["configurable"] = configurable

    return RunnableConfig(**config_kwargs)


async def streamed_assistant_message(content: str, config: RunnableConfig | None = None) -> AIMessage:
    """Build an assistant message the agent produced without a model call, streaming it as it is minted.

    A message returned from a middleware hook never passes through the model, so no
    ``on_chat_model_stream`` fires for it, and the chat's AG-UI translator has nothing to build
    ``TEXT_MESSAGE_*`` frames from. The reply then reaches the browser only inside the terminal
    ``MESSAGES_SNAPSHOT``, which ``chat-stream.js`` ignores, and the turn paints empty until the
    page is reloaded from the checkpoint. This event is what ``chat/api/streaming.py`` translates
    into those frames; every other transport ignores it.

    The frame and the returned message share an id, and that is load-bearing: ``_resumeRun``
    (``chat-stream.js``) seeds its replay dedup with the LangChain message ids of the hydrated
    checkpoint and skips frames carrying one. Mint an id here or reloading mid-run paints the
    message twice.

    ``config`` is passed explicitly wherever the hook is handed one — ``adispatch_custom_event``
    falls back to the ambient runnable config, but a failure to resolve it is swallowed below, so
    prefer the config we already hold over a silent miss. Streaming is best-effort: every graph
    invocation carries a parent run to dispatch against, so the guard only covers a caller running
    outside one, and a cosmetic frame must never fail the work that produced the message.
    """
    message_id = str(uuid.uuid4())
    message = AIMessage(id=message_id, content=content)
    try:
        await adispatch_custom_event(
            ASSISTANT_MESSAGE_EVENT, assistant_message_payload(message_id, content), config=config
        )
    except Exception:
        logger.warning("Could not stream an assistant message; it will surface on reload", exc_info=True)
    return message


def extract_body_from_frontmatter(frontmatter_text: str) -> str:
    """
    Extract prompt from text.

    Args:
        frontmatter_text (str): The frontmatter text to extract content from.

    Returns:
        str: The extracted content.
    """
    match = FRONTMATTER_RE.match(frontmatter_text)
    if not match:
        return frontmatter_text
    return frontmatter_text[match.end() :]
