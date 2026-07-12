"""Interleave per-run terminal-status markers into a build_turns() transcript.

Kept free of Django query code (it only reads attributes off already-fetched run
rows) so the segmentation logic is unit-testable without the database, mirroring
``chat.turns.build_turns``. The view/poller fetch runs (chronologically ordered)
and messages, then zip them here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from core.constants import CANCELLED_BY_USER_MESSAGE, INTERRUPTED_MESSAGE, RUN_FAILED_MESSAGE
from sessions.models import RunStatus, SessionOrigin

if TYPE_CHECKING:
    from sessions.models import Run

logger = logging.getLogger("daiv.sessions")

# Closed value set for a run-status marker's ``status`` key: ``failed`` renders the red
# error chip, ``aborted`` the neutral "stopped" one. Mirrored client-side in
# ``chat-stream.js`` (``_pushRunStatus``) — keep the two in sync. (Markers stay plain
# ``dict[str, Any]`` to interleave with ``chat.turns.build_turns`` output and serialise
# straight into the Alpine-rendered turns payload; the shape is ``id``/``role``/``status``/
# ``message``.)
RunStatusValue = Literal["failed", "aborted"]


def _marker(run: Run) -> dict[str, Any] | None:
    """Return a run-status pseudo-turn for a FAILED run, else None."""
    if run.status != RunStatus.FAILED:
        return None
    # error_message is only user-safe for chat runs: the chat streamer writes fixed
    # constants (see chat.api.streaming). Background/non-chat runs store raw exception
    # text and tracebacks in error_message via Run.mark_failed, which must never be
    # rendered verbatim. Fall those back to the generic sanitized message.
    if run.trigger_type == SessionOrigin.CHAT:
        message = run.error_message or RUN_FAILED_MESSAGE
        # A user cancel and an interrupt (stale takeover / process shutdown) are neutral,
        # non-failure terminations — render them as the grey "aborted" marker, not the red
        # failure chip. This is the single, deliberate string-coupling point: nothing
        # persisted distinguishes them from a genuine failure except these shared messages.
        aborted = run.error_message in (CANCELLED_BY_USER_MESSAGE, INTERRUPTED_MESSAGE)
    else:
        message = RUN_FAILED_MESSAGE
        aborted = False
    status: RunStatusValue = "aborted" if aborted else "failed"
    return {"id": f"run-status-{run.id}", "role": "run_status", "status": status, "message": message}


def _synthetic_turns(run: Run) -> list[dict[str, Any]]:
    """A run that produced no visible user turn. Recover a FAILED run's prompt as a
    user turn plus its marker; a non-failed run with no turn contributes nothing."""
    marker = _marker(run)
    if marker is None:
        return []
    out: list[dict[str, Any]] = []
    if run.prompt:
        out.append({"id": f"run-{run.id}", "role": "user", "segments": [{"type": "text", "content": run.prompt}]})
    out.append(marker)
    return out


def annotate_transcript(turns: list[dict[str, Any]], runs: list[Run]) -> list[dict[str, Any]]:
    """Splice run-status markers into ``turns`` at run boundaries.

    ``runs`` must be chronologically ordered. Runs are serial per session
    (SessionLock), so run *k* owns every turn from its user turn up to the next
    user turn. Matching is by ``message_id`` (chat runs) with a chronological
    ordinal fallback (background/legacy runs). A run whose ``message_id`` never
    reached the checkpoint is recovered via ``_synthetic_turns``.
    """
    # Segment the transcript at user-turn boundaries. Leading non-user turns (rare)
    # bucket into an anonymous head segment so nothing is dropped.
    segments: list[dict[str, Any]] = []
    for turn in turns:
        if turn.get("role") == "user" or not segments:
            segments.append({"user_id": turn.get("id") if turn.get("role") == "user" else None, "turns": [turn]})
        else:
            segments[-1]["turns"].append(turn)

    result: list[dict[str, Any]] = []
    cursor = 0  # index of the next unconsumed segment (ordinal fallback + skip-flushing)
    for run in runs:
        mid = run.message_id or ""
        matched = None
        if mid:
            for j in range(cursor, len(segments)):
                if segments[j]["user_id"] == mid:
                    matched = j
                    break
        elif cursor < len(segments):
            matched = cursor  # ordinal

        if matched is None:
            # No owning segment: pre-checkpoint failure (unmatched mid) or ran out of segments.
            # A FAILED run reaching here is the designed recovery path (its turn never
            # checkpointed); a non-failed run with a set message_id that matches nothing is
            # anomalous (a successful run should own a checkpointed turn) — log it so a
            # correlation regression is observable rather than a silently dropped turn.
            if mid and run.status != RunStatus.FAILED:
                logger.warning(
                    "annotate_transcript: run %s (status=%s) has message_id=%r but no matching "
                    "transcript segment; contributing no marker",
                    run.id,
                    run.status,
                    mid,
                )
            result.extend(_synthetic_turns(run))
            continue

        # Flush any segments with no owning run (defensive; keeps ordering intact).
        while cursor < matched:
            result.extend(segments[cursor]["turns"])
            cursor += 1
        result.extend(segments[matched]["turns"])
        cursor = matched + 1
        if (marker := _marker(run)) is not None:
            result.append(marker)

    # Flush trailing segments with no owning run.
    while cursor < len(segments):
        result.extend(segments[cursor]["turns"])
        cursor += 1
    return result
