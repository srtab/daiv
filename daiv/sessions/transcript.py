"""Interleave per-run terminal-status markers into a build_turns() transcript.

``annotate_transcript`` is kept free of Django query code (it only reads attributes off
already-fetched run rows) so the segmentation logic is unit-testable without the database,
mirroring ``sessions.turns.build_turns``. The view/poller fetch runs (chronologically ordered)
and messages, then zip them here; for a checkpoint with no messages, ``expired_run_turns``
restores the prompts and queries the artifacts it no longer carries. ``retry_message`` reads the
annotated turns for what the newest run's Retry sends.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from django.urls import reverse

from skills.models import GlobalSkill, SkillInvocation

from automation.agent.artifacts import PUBLISH_ARTIFACT_TOOL_NAME
from automation.agent.constants import SKILLS_TOOL_NAME
from automation.agent.delegation import DELEGATE_JOBS_TOOL_NAME
from core.constants import (
    CANCELLED_BY_COORDINATOR_MESSAGE,
    CANCELLED_BY_USER_MESSAGE,
    INTERRUPTED_MESSAGE,
    RUN_FAILED_MESSAGE,
)
from sessions.artifacts import published_tool_result
from sessions.executor.lock import SessionActivity
from sessions.models import (
    CHAT_PROMPT_MAX_LENGTH,
    PublishErrorKind,
    PublishStatus,
    Run,
    RunArtifact,
    RunStatus,
    SessionOrigin,
)
from sessions.turns import tool_call_segment

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from sessions.delegation import Delegations

logger = logging.getLogger("daiv.sessions")

RETRY_MESSAGE = "The previous run failed. Please try again."

# Closed value set for a run-status marker's ``status`` key: ``failed`` renders the red
# error chip, ``aborted`` the neutral "stopped" one. Mirrored client-side in
# ``chat-stream.js`` (``_pushRunStatus``) — keep the two in sync. (Markers stay plain
# ``dict[str, Any]`` to interleave with ``sessions.turns.build_turns`` output and serialise
# straight into the Alpine-rendered turns payload; the shape is ``id``/``role``/``status``/
# ``message``.)
RunStatusValue = Literal["failed", "aborted"]


def _marker(run: Run) -> dict[str, Any] | None:
    """Return a run-status pseudo-turn for a FAILED run, else None."""
    if run.status != RunStatus.FAILED:
        return None
    # error_message is only user-safe for chat runs: the chat streamer writes fixed
    # constants (see chat.api.streaming). Background/non-chat runs store raw exception
    # text and tracebacks in error_message (sessions.queue), which must never be
    # rendered verbatim. Fall those back to the generic sanitized message.
    if run.trigger_type == SessionOrigin.CHAT:
        message = run.error_message or RUN_FAILED_MESSAGE
        # A user cancel and an interrupt (stale takeover / process shutdown) are neutral,
        # non-failure terminations — render them as the grey "aborted" marker, not the red
        # failure chip. This is the single, deliberate string-coupling point: nothing
        # persisted distinguishes them from a genuine failure except these shared messages.
        aborted = run.error_message in (CANCELLED_BY_USER_MESSAGE, INTERRUPTED_MESSAGE)
    elif run.is_cancelled:
        message, aborted = CANCELLED_BY_COORDINATOR_MESSAGE, True
    else:
        message = RUN_FAILED_MESSAGE
        aborted = False
    status: RunStatusValue = "aborted" if aborted else "failed"
    return {"id": marker_id(run), "role": "run_status", "status": status, "message": message}


def marker_id(run: Run) -> str:
    """The id of ``run``'s run-status marker; the live page names the marker it pushes the same way."""
    return f"run-status-{run.id}"


def _red_line_replaces(marker: dict[str, Any], run: Run) -> bool:
    """Whether ``run``'s failed publish line stands in for its ``marker``; a stop keeps its own marker below it."""
    return marker["status"] == "failed" and run.publish_status == PublishStatus.FAILED


def _publish_entry(run: Run, status: str, *, error: str = "") -> dict[str, Any]:
    """Only a publish that landed shows the run's merge request and figures, as the live line does."""
    entry = {
        "status": str(status),
        "merge_request_iid": None,
        "merge_request_web_url": "",
        "files_changed": None,
        "lines_added": None,
        "lines_removed": None,
        "error": error,
    }
    if status in (PublishStatus.OPENED, PublishStatus.PUSHED):
        entry |= {
            "merge_request_iid": run.merge_request_iid,
            "merge_request_web_url": run.merge_request_web_url or "",
            "files_changed": run.files_changed,
            "lines_added": run.lines_added,
            "lines_removed": run.lines_removed,
        }
    return entry


def _publish_error(run: Run) -> str:
    if not run.publish_error:
        return ""
    try:
        kind = PublishErrorKind(run.publish_error)
    except ValueError:
        kind = PublishErrorKind.UNKNOWN
    return str(kind.label)


def _publish_entries(run: Run) -> list[dict[str, Any]]:
    """The entries of ``run``'s publish result; a failed publish is the red line and carries the retry anchor, followed
    by the draft recovery opened after it."""
    if not run.publish_status:
        return []
    entry = _publish_entry(run, run.publish_status, error=_publish_error(run))
    if run.publish_status != PublishStatus.FAILED:
        return [entry]
    entries = [{"id": marker_id(run), **entry}]
    if run.code_changes and run.merge_request_iid:
        entries.append(_publish_entry(run, PublishStatus.OPENED))
    return entries


def _stamp_publish(run_turns: list[dict[str, Any]], run: Run) -> None:
    """Stamp ``run``'s publish result on its last assistant turn, appending an empty one to carry it when none."""
    entries = _publish_entries(run)
    if not entries:
        return
    carrier = next((turn for turn in reversed(run_turns) if turn.get("role") == "assistant"), None)
    if carrier is None:
        carrier = {"id": f"run-{run.id}-publish", "role": "assistant", "segments": []}
        run_turns.append(carrier)
    carrier["publish"] = entries


def _stamp_prompt_times(user_turn: dict[str, Any], run: Run) -> None:
    """Stamp ``sent_at`` (creation time) and ``started_at`` (execution start) on ``run``'s prompt turn."""
    if run.created_at is not None:
        user_turn["sent_at"] = run.created_at.isoformat()
    if run.started_at is not None:
        user_turn["started_at"] = run.started_at.isoformat()


def _stamp_run_times(seg_turns: list[dict[str, Any]], run: Run) -> None:
    """Stamp run-level timings onto a segment's boundary turns (machine ISO-8601).

    ``sent_at`` (creation time) and ``started_at`` (execution start) land on the owning user turn — the segment's
    first turn when it is a user turn — and ``received_at`` (finish time) on the run's last turn, once finished.
    """
    if not seg_turns:
        return
    if seg_turns[0].get("role") == "user":
        _stamp_prompt_times(seg_turns[0], run)
    if run.finished_at is not None:
        seg_turns[-1]["received_at"] = run.finished_at.isoformat()


def _mark_author(user_turn: dict[str, Any], run: Run) -> None:
    """Tag a user turn whose prompt DAIV wrote, so the page shows it as DAIV's notice, not the user's message."""
    if run.trigger_type in SessionOrigin.daiv_authored():
        user_turn["author"] = "daiv"
        user_turn["origin"] = run.trigger_type


def _prompt_turns(run: Run, *, pending: bool = False) -> list[dict[str, Any]]:
    """``run``'s prompt as a user turn, flagged ``pending`` while the run waits to start; none without a prompt."""
    if not run.prompt:
        return []
    user_turn = {"id": f"run-{run.id}", "role": "user", "segments": [{"type": "text", "content": run.prompt}]}
    _stamp_prompt_times(user_turn, run)
    if pending:
        user_turn["pending"] = True
    _mark_author(user_turn, run)
    return [user_turn]


def _synthetic_turns(run: Run) -> list[dict[str, Any]]:
    """A run that produced no visible user turn. Recover a FAILED run's prompt as a
    user turn plus its marker, flagged ``rebuilt``; a non-failed run with no turn contributes nothing."""
    marker = _marker(run)
    if marker is None:
        return []
    run_turns = _prompt_turns(run)
    _stamp_publish(run_turns, run)
    _stamp_run_times(run_turns, run)
    if not _red_line_replaces(marker, run):
        run_turns.append(marker)
    return [{**turn, "rebuilt": True} for turn in run_turns]


def _anchor(run_turns: list[dict[str, Any]], run: Run) -> list[dict[str, Any]]:
    """Stamp ``run_id`` on the first of ``run_turns``: the page renders it as the run's ``#run-<id>`` link target."""
    if run_turns:
        run_turns[0]["run_id"] = str(run.id)
    return run_turns


def _unstarted(run: Run, activity: SessionActivity) -> bool:
    """Whether ``run`` has not executed yet, so it owns no checkpointed turn: never picked up, or behind the slot's
    holder."""
    return run.status in (RunStatus.READY, RunStatus.QUEUED) or activity.behind_holder(run)


def annotate_transcript(
    turns: list[dict[str, Any]], runs: list[Run], activity: SessionActivity | None = None
) -> list[dict[str, Any]]:
    """Splice run-status markers into ``turns`` at run boundaries.

    ``runs`` must be chronologically ordered. Runs are serial per session
    (SessionLock), so run *k* owns every turn from its user turn up to the next
    user turn. Matching is by ``message_id`` (chat runs) with a chronological
    ordinal fallback (background/legacy runs). A run whose ``message_id`` never
    reached the checkpoint is recovered via ``_synthetic_turns``. Each run's first turn carries its ``run_id``.
    ``activity`` says which unfinished runs have not started, so they own no turn, and which of them show their
    prompt as pending; it defaults to nothing in flight.
    """
    activity = activity or SessionActivity()
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
        if run in activity.waiting:
            result.extend(_anchor(_prompt_turns(run, pending=True), run))
            continue
        if _unstarted(run, activity):
            continue
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
            # A FAILED run's or the holder's turn never checkpointed (or not yet); a finished run's message_id
            # should match one, so warn to keep a correlation regression visible.
            if mid and run.status in RunStatus.terminal() - {RunStatus.FAILED}:
                logger.warning(
                    "annotate_transcript: run %s (status=%s) has message_id=%r but no matching "
                    "transcript segment; contributing no marker",
                    run.id,
                    run.status,
                    mid,
                )
            result.extend(_anchor(_prompt_turns(run) if run == activity.holder else _synthetic_turns(run), run))
            continue

        # Flush any segments with no owning run (defensive; keeps ordering intact).
        while cursor < matched:
            result.extend(segments[cursor]["turns"])
            cursor += 1
        # received_at targets the segment's own turns; the run_status marker is appended
        # to `result` separately below, so it never receives a timestamp.
        owned = _anchor(segments[matched]["turns"], run)
        _stamp_publish(owned, run)
        _stamp_run_times(owned, run)
        if owned[0].get("role") == "user":
            _mark_author(owned[0], run)
        result.extend(owned)
        cursor = matched + 1
        if (marker := _marker(run)) is not None and not _red_line_replaces(marker, run):
            result.append(marker)

    # Flush trailing segments with no owning run.
    while cursor < len(segments):
        result.extend(segments[cursor]["turns"])
        cursor += 1
    return result


def _artifact_segment(artifact: RunArtifact) -> dict[str, Any]:
    segment = tool_call_segment({"id": f"artifact-{artifact.id}", "name": PUBLISH_ARTIFACT_TOOL_NAME})
    return {**segment, "result": published_tool_result(artifact)}


def expired_run_turns(runs: list[Run], shown: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """SYNC ONLY: what a checkpoint with no messages still shows of each run, from rows that outlive its TTL.

    A finished run no ``shown`` turn carries gets its prompt back, and each run that published gets one assistant
    turn of synthetic ``publish_artifact`` segments (artifacts oldest first) shaped like ``build_turns`` output.
    A run's first turn carries its ``run_id`` unless a ``shown`` turn already does.
    """
    anchored = {turn.get("run_id") for turn in shown}
    by_run: dict[Any, list[dict[str, Any]]] = {}
    for artifact in RunArtifact.objects.filter(run__in=runs).select_related("run"):
        by_run.setdefault(artifact.run_id, []).append(_artifact_segment(artifact))
    result = []
    for run in runs:
        unshown = str(run.id) not in anchored
        run_turns = _prompt_turns(run) if unshown and run.status in RunStatus.terminal() else []
        if run.id in by_run:
            run_turns.append({"id": f"run-{run.id}-artifacts", "role": "assistant", "segments": by_run[run.id]})
        if unshown:
            _stamp_publish(run_turns, run)
            _stamp_run_times(run_turns, run)
            _anchor(run_turns, run)
        result.extend(run_turns)
    return result


def retry_message(turns: list[dict[str, Any]], runs: list[Run]) -> str:
    """What Retry on the newest of ``runs`` sends, for ``turns`` annotated from them; empty when it offers none.

    Offered on a run that failed rather than stopped, from a ``SessionOrigin.retryable`` trigger. Retry posts a chat
    turn, which hands the agent only its own message: a run whose turns are in the checkpoint gets
    ``RETRY_MESSAGE``, and one ``rebuilt`` from its row has its prompt sent again.
    """
    if not runs:
        return ""
    run = runs[-1]
    marker = _marker(run)
    if marker is None or marker["status"] != "failed" or run.trigger_type not in SessionOrigin.retryable():
        return ""
    run_id = str(run.id)
    anchor = next((turn for turn in turns if turn.get("run_id") == run_id), None)
    if anchor is not None and not anchor.get("rebuilt"):
        return RETRY_MESSAGE
    # A chat run's stored prompt is cut at the cap, so one that long may not be the whole message.
    if run.trigger_type == SessionOrigin.CHAT and len(run.prompt) >= CHAT_PROMPT_MAX_LENGTH:
        return ""
    return run.prompt


def _tool_args(segment: dict[str, Any]) -> dict[str, Any]:
    try:
        args = json.loads(segment.get("args") or "")
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}


def _tool_calls(turns: list[dict[str, Any]], name: str) -> Iterator[dict[str, Any]]:
    for turn in turns:
        for segment in turn.get("segments", ()):
            if segment.get("type") == "tool_call" and segment.get("name") == name:
                yield segment


def _skill_name(segment: dict[str, Any]) -> str | None:
    args = _tool_args(segment)
    name = args.get("skill") or args.get("name")
    return name if isinstance(name, str) and name else None


def mark_skill_links(turns: list[dict[str, Any]], thread_id: str | UUID) -> None:
    """Set ``skill_url`` on skill tool calls whose skill has a Skills page (built-in or global).

    One query for the whole transcript, plus one when a global skill is named, which must still exist; the latest
    invocation in the thread decides the source. Sync only.
    """
    named = [(segment, name) for segment in _tool_calls(turns, SKILLS_TOOL_NAME) if (name := _skill_name(segment))]
    if not named:
        return
    try:
        UUID(str(thread_id))
    except ValueError:
        return
    latest: dict[str, str] = {}
    rows = SkillInvocation.objects.filter(thread_id=thread_id, name__in={n for _, n in named})
    for name, source in rows.order_by("created").values_list("name", "source"):
        latest[name] = source
    linkable = {name for name, source in latest.items() if source == SkillInvocation.Source.BUILTIN}
    if global_names := [name for name, source in latest.items() if source == SkillInvocation.Source.GLOBAL]:
        linkable.update(GlobalSkill.objects.filter(name__in=global_names).values_list("name", flat=True))
    for segment, name in named:
        if name in linkable:
            segment["skill_url"] = reverse("skills:detail", args=[name])


def _batch_id(segment: dict[str, Any]) -> UUID | None:
    try:
        result = json.loads(segment.get("result") or "")
        return UUID(result["batch_id"])
    except ValueError, TypeError, KeyError, AttributeError:
        return None


def _goal(segment: dict[str, Any]) -> str:
    goal = _tool_args(segment).get("goal")
    return goal.strip() if isinstance(goal, str) else ""


def mark_delegations(turns: list[dict[str, Any]], delegations: Delegations) -> dict[UUID, str]:
    """Set ``legs`` on ``delegate_jobs`` tool calls from ``delegations``, which wins over the tool result, and return
    the ``goal`` each of those calls gave, by batch, for the calls that gave one.

    A call whose batch has no legs in ``delegations`` (none of the session's own) is left as it was.
    """
    goals: dict[UUID, str] = {}
    if not delegations.legs:
        return goals
    for segment in _tool_calls(turns, DELEGATE_JOBS_TOOL_NAME):
        batch_id = _batch_id(segment)
        if batch_id is None or batch_id not in delegations.legs:
            continue
        segment["legs"] = delegations.legs[batch_id]
        if goal := _goal(segment):
            goals[batch_id] = goal
    return goals
