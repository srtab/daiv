"""The merge request a thread's checkpoint records, read back and resolved into the one a run publishes to.

Kept free of the agent stack so the sessions app can read a checkpoint's merge request at import time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from codebase.base import MergeRequest

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger("daiv.tools")


def checkpointed_merge_request(state: Mapping[str, Any], *, strict: bool) -> MergeRequest | None:
    """Read ``state["merge_request"]``, rejecting a value that didn't revive to a ``MergeRequest``.

    ``DAIVRedisSerializer`` hands back the raw ``lc:2`` envelope ``dict`` when reconstruction fails
    (a schema drift across a deploy), and downstream ``mr.source_branch`` would then raise far from
    the cause. ``strict`` picks the policy: raise where the run can still be failed cleanly, or
    degrade to ``None`` on a last-chance path whose own catch-all would swallow the error and
    discard the work it exists to save.
    """
    merge_request = state.get("merge_request")
    if merge_request is None or isinstance(merge_request, MergeRequest):
        return merge_request
    logger.error(
        "Checkpointed merge_request revived as %s, not a MergeRequest — likely a stale checkpoint "
        "after a MergeRequest schema change.",
        type(merge_request).__name__,
    )
    if strict:
        raise TypeError(f"Checkpointed merge_request revived as {type(merge_request).__name__}, expected MergeRequest")
    return None


def effective_merge_request(
    *, context_mr: MergeRequest | None, state_mr: MergeRequest | None, current_ref: str
) -> MergeRequest | None:
    """The MR a run may advertise in its prompt and add commits onto, or ``None`` for a fresh one.

    Prefer the context MR (MR-scope runs), then the checkpointed one while its ``source_branch`` is
    still the checked-out ref. Publishing onto a branch this workspace is not on pushes a sibling of
    that branch's tip, and the rejected push rebases into a conflict that discards the turn's work —
    so a redundant MR is the cheap failure here and a stale publish target the expensive one.

    ``current_ref`` is the ref the *clone* is on — whether this workspace's history descends from
    the MR's branch — not where in-sandbox git points, which the sandbox policy keeps the agent from switching.

    Every caller that hands a ``merge_request`` to ``GitChangePublisher.publish`` resolves it
    through this, the post-error draft recovery included.
    """
    if context_mr is not None:
        return context_mr
    if state_mr is not None and state_mr.source_branch == current_ref:
        return state_mr
    return None
