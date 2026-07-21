from __future__ import annotations

from typing import TYPE_CHECKING, Any

from codebase.base import MergeRequestState
from sessions.models import RunEnvelope

if TYPE_CHECKING:
    from sessions.models import Run

# Sentinel distinguishing "caller passed no envelope, look it up" (``_UNSET``) from "caller asserts
# there is no envelope — the run is still classifying" (an explicit ``None``). Letting ``None`` mean
# classifying lets the badge/Feed pass their already-batched per-run envelope directly (``None`` for
# a run with no envelope) with no re-query, so no N+1 is introduced. Typed ``Any`` (matching the
# house sentinel idiom) so the default is assignable to the ``RunEnvelope | None`` parameter.
_UNSET: Any = object()


def still_actionable(run: Run, envelope: RunEnvelope | None = _UNSET) -> bool:
    """The single shared liveness predicate (AC2) — read-only (AC3), fail-safe (AC6).

    Composes two checks and NEVER writes (no ``.save()`` / ``aupdate`` / signal):

    1. **Classification actionability.** ``envelope is _UNSET`` → look it up via
       ``RunEnvelope.objects.for_run(run)``. An explicit ``None`` (or a resolved ``None``) means the
       run is still *classifying* → actionable until it resolves. Otherwise defer to
       ``envelope.is_actionable`` (an ``all-clear`` envelope is not actionable). Because
       ``RunEnvelope.clean()`` enforces ``is_actionable ⟺ status != all-clear`` for every persisted
       envelope, this preserves the badge's existing counted cases.
    2. **Live MR resolution.** Only when the item references an MR (``run.merge_request_iid`` is set)
       do we consult the Task-2 cached live read: a confirmed ``MERGED`` / ``CLOSED`` state means the
       MR was resolved externally → *not* actionable (AC4/AC5); ``OPEN`` / ``DRAFT``, an unknown
       state, or a failed read keeps it actionable (AC6, the fail-safe: an item leaves only on a
       confirmed resolution). A run with no ``merge_request_iid`` skips the live read entirely.
    """
    env = RunEnvelope.objects.for_run(run) if envelope is _UNSET else envelope
    actionable = True if env is None else env.is_actionable
    if not actionable or run.merge_request_iid is None:
        return actionable
    # Lazy import: keeps the sessions <-> codebase dependency acyclic at import time.
    from codebase.mr_state import get_merge_request_state

    # ``actionable`` is necessarily True here (the guard above returned otherwise), so liveness is
    # decided solely by the MR state: a confirmed resolution drops the item, anything else keeps it.
    state = get_merge_request_state(run.repo_id, run.merge_request_iid)
    return state not in MergeRequestState.resolved()
