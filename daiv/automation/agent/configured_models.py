"""Build a skill's or subagent's configured model (``AgentSettings.skill_models`` / ``subagent_models``), and report
the fallback when it cannot be built."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from automation.agent.base import get_model
from automation.agent.notices import ModelFallbackNotice

if TYPE_CHECKING:
    from collections.abc import Mapping

    from langchain.chat_models import BaseChatModel

    from automation.agent.base import ThinkingLevel
    from automation.agent.notices import FallbackReason, NoticeTarget, RunNotices

logger = logging.getLogger("daiv.agent")


def build_configured_model(
    *, spec: str, thinking_level: ThinkingLevel | None
) -> tuple[BaseChatModel | None, FallbackReason | None]:
    """The model ``spec`` names, or ``None`` and why it cannot be used.

    The caller reports the fallback through ``report_model_fallback`` once it knows which model served.
    """
    try:
        return get_model(model=spec, thinking_level=thinking_level), None
    except ValueError:
        return None, "unknown_provider"
    except RuntimeError:
        return None, "provider_unavailable"
    except Exception:
        # Safe to continue: the caller serves on the model it would use with no configured entry and reports it.
        logger.exception("Configured model '%s' failed to initialize", spec)
        return None, "init_failed"


def report_model_fallback(
    notices: RunNotices | None,
    *,
    target: NoticeTarget,
    name: str,
    configured_model: str,
    served_model: str,
    reason: FallbackReason,
) -> None:
    """Log a configured model's fallback at ERROR and record it on ``notices``, when the run has them."""
    logger.error(
        "Configured %s model unusable: name=%s configured_model=%s served_model=%s reason=%s",
        target,
        name,
        configured_model,
        served_model,
        reason,
    )
    if notices is not None:
        notices.record(
            ModelFallbackNotice(
                target=target, name=name, configured_model=configured_model, served_model=served_model, reason=reason
            )
        )


def configured_subagent_model(
    name: str,
    *,
    file_spec: str,
    configured_models: Mapping[str, str],
    thinking_level: ThinkingLevel | None,
    run_model: BaseChatModel,
    run_model_spec: str,
    notices: RunNotices | None,
) -> BaseChatModel | None:
    """The model subagent ``name`` runs on when it has a configured entry; ``None`` when it has none.

    An entry that cannot be built falls back to ``file_spec``, else the run's model, so the subagent is never left out.
    """
    if not (configured := configured_models.get(name)):
        return None
    model, reason = build_configured_model(spec=configured, thinking_level=thinking_level)
    if reason is None:
        return model
    served, served_spec = run_model, run_model_spec
    if file_spec:
        try:
            served, served_spec = get_model(model=file_spec), file_spec
        except Exception:
            # Safe to continue: the run's model serves, and the fallback is reported below.
            logger.warning("Subagent %s: its file's model '%s' is unusable too", name, file_spec, exc_info=True)
    report_model_fallback(
        notices, target="subagent", name=name, configured_model=configured, served_model=served_spec, reason=reason
    )
    return served
