from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents.middleware import SummarizationMiddleware
from deepagents.middleware.summarization import compute_summarization_defaults

from automation.agent.usage_tracking import resolve_window_by_name

if TYPE_CHECKING:
    from deepagents.backends import BackendProtocol
    from langchain.chat_models import BaseChatModel

COMPACTION_WINDOW_FRACTION = 0.85


def build_summarization_middleware(model: BaseChatModel, backend: BackendProtocol) -> SummarizationMiddleware:
    """deepagents' summarization, without tool-argument truncation whenever the model's context window is known.

    Truncation clips large tool-call arguments in messages already sent, one message at a time as each
    ages out of its keep window, so the request changes behind the newest turn on many calls: every
    change restarts the prompt cache from there, and Claude models that bind thinking blocks to the
    exact history reject or drop them. With the window known, compaction is set to fire before the
    window fills and truncation is turned off, so history only changes when compaction runs.

    deepagents reads the window from the model's profile only, which OpenRouter models lack; for those
    the window comes from DAIV's model catalog, and compaction moves from the fixed token trigger to
    ``COMPACTION_WINDOW_FRACTION`` of the window (deepagents' own point for profiled models) when that
    is lower. A model whose window is unknown keeps deepagents' defaults, truncation included, since
    truncation is then what holds off an overflow.

    The instance reports deepagents' own ``SummarizationMiddleware`` name, so passing it to
    ``create_deep_agent`` replaces the default one in place.
    """
    defaults = compute_summarization_defaults(model)
    trigger = defaults["trigger"]
    truncate_args_settings = defaults["truncate_args_settings"]
    if trigger[0] == "fraction":
        truncate_args_settings = None
    elif window := _window_by_name(model):
        trigger = ("tokens", min(trigger[1], int(window * COMPACTION_WINDOW_FRACTION)))
        truncate_args_settings = None

    return SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=trigger,
        keep=defaults["keep"],
        trim_tokens_to_summarize=None,
        truncate_args_settings=truncate_args_settings,
    )


def _window_by_name(model: BaseChatModel) -> int | None:
    """The context window DAIV's model catalog lists for ``model``, or ``None`` when it lists none."""
    name = getattr(model, "model_name", None) or getattr(model, "model", None)
    if not isinstance(name, str):
        return None
    resolved = resolve_window_by_name(name)
    return resolved.tokens if resolved else None
