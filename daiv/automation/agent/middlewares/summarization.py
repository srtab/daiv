from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents.middleware import SummarizationMiddleware
from deepagents.middleware.summarization import compute_summarization_defaults

if TYPE_CHECKING:
    from deepagents.backends import BackendProtocol
    from langchain.chat_models import BaseChatModel


def build_summarization_middleware(model: BaseChatModel, backend: BackendProtocol) -> SummarizationMiddleware:
    """deepagents' summarization with its model-aware thresholds, but without tool-argument truncation.

    Truncation clips large tool-call arguments in messages already sent, one message at a time as each
    ages out of its keep window, so the request changes behind the newest turn on many calls: every
    change restarts the prompt cache from there, and Claude models that bind thinking blocks to the
    exact history reject or drop them. Without it, history only changes when compaction runs.

    The instance reports deepagents' own ``SummarizationMiddleware`` name, so passing it to
    ``create_deep_agent`` replaces the default one in place.
    """
    defaults = compute_summarization_defaults(model)
    return SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=defaults["trigger"],
        keep=defaults["keep"],
        trim_tokens_to_summarize=None,
        truncate_args_settings=None,
    )
