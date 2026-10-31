"""What the model receives for an inbox run (``specs/001-session-inbox/contracts/agent-input.md``).

Two messages, built from the events the run took:

- the **context**, a ``synthetic_message(kind="inbox")`` the transcript hides: one line per remembered event, oldest
  first, an event a newer one of the same subject replaced shown with ``Superseded by …`` lines beneath it. Capped at
  ``CONTEXT_MAX_CHARS``: past it, the oldest remembered lines collapse into ``N more <kind> events``. Omitted when
  there is nothing to report.
- the **request**, a ``HumanMessage``: people's words when the run took any (chat messages, mentions with their
  authors, the follow-up job's prompt), then DAIV's own sections (a delegated batch's summary, the CI fix prompt), in
  precedence order and separated by a blank line.

Everything here is addressed to the model, so none of it is translated. The owner's identity never appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
from operator import attrgetter
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from sessions.inbox.kinds import PRECEDENCE
from sessions.models import EventKind, EventState, InboxEvent, QuietReason

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from langchain_core.messages import HumanMessage

    from sessions.models import Run

INBOX_CONTEXT_KIND = "inbox"
CONTEXT_MAX_CHARS = 8000
QUOTE_MAX_CHARS = 1000
CONTEXT_HEADER = "Since your last run on this session:"
MENTION_REPLY_NOTE = "Reply as you would on the merge request; your final message is posted there."

FIX_RUN_PROMPT = (
    "CI failed on this merge request. Failed jobs: {names}. Pipeline: {url}\n\n"
    "Read the job logs to find the cause, then fix it. If the failure is not something a "
    "code change can fix, explain why instead of guessing."
)

_QUIET_REASONS = {
    QuietReason.NOT_DAIV_COMMIT: "the commit is not one you pushed",
    QuietReason.ALREADY_EXAMINED: "you already examined this commit",
    QuietReason.BUDGET_SPENT: "the CI fix attempts are spent",
    QuietReason.WATCH_OFF: "the CI watch is off",
    QuietReason.MUTED: "the session is muted",
    QuietReason.RUN_LIMIT: "too many runs in a row started without a person",
}
_CI_STATUS = {EventKind.CI_FAILED: "FAILED", EventKind.CI_PASSED: "PASSED", EventKind.CI_STOPPED: "STOPPED"}
_DELEGATED_OUTCOMES = {
    "passing": "passing",
    "stopped_after_attempts": "still failing after the fix attempts",
    "no_push": "failed, and the run that looked at it pushed nothing",
}


@dataclass(frozen=True)
class RenderedInput:
    context: HumanMessage | None
    request: HumanMessage

    @property
    def messages(self) -> tuple[HumanMessage, ...]:
        """The run's input, context first."""
        return (self.request,) if self.context is None else (self.context, self.request)


def render_input(run: Run) -> RenderedInput:
    """The input for ``run`` from the events it took. SYNC ONLY."""
    # Deferred: ``sessions.signals`` imports this module at app load, and the agent stack must not load with it.
    from langchain_core.messages import HumanMessage

    from automation.agent.synthetic import synthetic_message

    events = list(InboxEvent.objects.filter(run=run).select_related("author").order_by("created_at", "id"))
    context = render_context(events)
    return RenderedInput(
        context=synthetic_message(context, kind=INBOX_CONTEXT_KIND) if context else None,
        request=HumanMessage(content=render_request(events), id=run.message_id or None),
    )


arender_input = sync_to_async(render_input)


def render_request(events: Iterable[InboxEvent]) -> str:
    """The request sections for the waking events among ``events``, in precedence order."""
    waking = sorted(
        (event for event in events if event.wake and event.state != EventState.SUPERSEDED),
        key=lambda event: (PRECEDENCE[event.kind], event.created_at),
    )
    sections = [
        _section(kind, list(group)) for kind, group in groupby(waking, key=attrgetter("kind")) if kind in PRECEDENCE
    ]
    return "\n\n".join(section for section in sections if section)


def _section(kind: str, events: list[InboxEvent]) -> str:
    if kind == EventKind.CHAT_MESSAGE:
        if len(events) == 1:
            return events[0].payload["text"]
        return "\n\n".join(f"[{event.created_at:%H:%M}] {event.payload['text']}" for event in events)
    if kind == EventKind.MENTION:
        mentions = [
            f"{_author(event)} mentioned you on !{event.merge_request_iid} ({event.payload['web_url']}):\n"
            f"{_blockquote(event.payload['body'])}"
            for event in events
        ]
        return "\n\n".join([*mentions, MENTION_REPLY_NOTE])
    if kind == EventKind.FOLLOW_UP_JOB:
        return "\n\n".join(event.payload["prompt"] for event in events)
    if kind == EventKind.DELEGATION_FINISHED:
        return "\n\n".join(event.payload["summary"] for event in events)
    return "\n\n".join(
        FIX_RUN_PROMPT.format(
            names=", ".join(event.payload["failed_jobs"]) or "the pipeline", url=event.payload["web_url"]
        )
        for event in events
    )


@dataclass
class _Entry:
    position: int
    lines: list[str]
    kind: str
    collapsible: bool


def render_context(events: Sequence[InboxEvent]) -> str:
    """The context message's text for ``events``, or ``""`` when no line is due."""
    position = {event.pk: index for index, event in enumerate(events)}
    chained = list(dict.fromkeys(e.subject for e in events if e.state == EventState.SUPERSEDED and e.subject))
    entries: list[_Entry] = []
    for subject in chained:
        chain = [event for event in events if event.subject == subject]
        entries.append(
            _Entry(
                position=position[chain[0].pk],
                lines=[_line(chain[0]), *(f"  Superseded by {_short(event)}." for event in chain[1:])],
                kind=chain[-1].kind,
                collapsible=not chain[-1].wake,
            )
        )
    entries.extend(
        _Entry(position=position[event.pk], lines=[_line(event)], kind=event.kind, collapsible=True)
        for event in events
        if not event.wake and event.state != EventState.SUPERSEDED and event.subject not in chained
    )
    if not entries:
        return ""
    return _fit(sorted(entries, key=attrgetter("position")))


def _fit(entries: list[_Entry]) -> str:
    collapsed: dict[str, int] = {}
    kept = list(entries)

    def _text() -> str:
        summary = [f"- {count} more {kind.replace('_', ' ')} events" for kind, count in collapsed.items()]
        body = [line for entry in kept for line in entry.lines]
        return "\n".join([CONTEXT_HEADER, "", *summary, *body])

    while len(_text()) > CONTEXT_MAX_CHARS:
        oldest = next((entry for entry in kept if entry.collapsible), None)
        if oldest is None:
            break
        kept.remove(oldest)
        collapsed[oldest.kind] = collapsed.get(oldest.kind, 0) + 1
    return _text()


SUMMARY_MAX_CHARS = 140


def summarize(event: InboxEvent) -> str:
    """A one-line summary of ``event`` for the session page, built from its payload alone."""
    payload = event.payload
    match event.kind:
        case EventKind.CHAT_MESSAGE:
            text = payload["text"]
        case EventKind.MENTION:
            text = f"{_author(event)} on !{event.merge_request_iid}: {payload['body']}"
        case EventKind.FOLLOW_UP_JOB:
            text = payload["prompt"]
        case EventKind.DELEGATION_FINISHED:
            text = "Delegated jobs finished"
        case EventKind.CI_FAILED | EventKind.CI_PASSED | EventKind.CI_STOPPED:
            text = f"CI on !{event.merge_request_iid}: pipeline {payload['pipeline_id']} {_ci_status(event).lower()}"
        case _:
            text = _describe(event)
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_MAX_CHARS else f"{text[: SUMMARY_MAX_CHARS - 1]}…"


def _line(event: InboxEvent) -> str:
    return f"- {_describe(event)}"


def _describe(event: InboxEvent) -> str:
    payload = event.payload
    match event.kind:
        case EventKind.CI_FAILED | EventKind.CI_PASSED | EventKind.CI_STOPPED:
            jobs = f", jobs: {', '.join(payload['failed_jobs'])}" if payload.get("failed_jobs") else ""
            text = (
                f"CI on !{event.merge_request_iid} (branch {event.ref}): pipeline {payload['pipeline_id']} "
                f"{_ci_status(event)} on {payload['sha'][:8]}{jobs}. {payload['web_url']}"
            )
            return f"{text} {_not_woken(event)}" if event.reason else text
        case EventKind.BRANCH_PUSHED:
            count = payload["commit_count"]
            return (
                f"The branch {event.ref} moved: {payload['pusher_username']} pushed {count} "
                f"{'commit' if count == 1 else 'commits'} ({payload['before'][:8]} → {payload['after'][:8]})."
            )
        case EventKind.FOREIGN_PUBLISH:
            return (
                f"{payload['owner_username']} published {payload['sha'][:8]} to the branch {event.ref} from a session "
                "of their own; this session still owns the merge request."
            )
        case EventKind.MR_MERGED:
            return f"!{event.merge_request_iid} was merged into {payload['target_branch']} by {payload['by_username']}."
        case EventKind.MR_CLOSED:
            return f"!{event.merge_request_iid} was closed by {payload['by_username']}."
        case EventKind.COMMENT:
            return (
                f"Comment by {_author(event)} on !{event.merge_request_iid} (not addressed to you): "
                f'"{_cut(payload["body"])}"'
            )
        case EventKind.DELEGATED_CI_OUTCOME:
            outcome = _DELEGATED_OUTCOMES.get(payload["outcome"], payload["outcome"])
            return (
                f"Delegated session {payload['child_thread_id'][:8]}… (!{payload['merge_request_iid']}): CI {outcome}."
            )
        case EventKind.DELEGATION_FINISHED:
            return f"Delegated jobs finished. {_not_woken(event)}\n  {_cut(payload['summary'])}"
        case EventKind.NOTICE:
            return payload["text"]
    return f"{event.get_kind_display()}."


def _short(event: InboxEvent) -> str:
    if event.kind in _CI_STATUS:
        return f"pipeline {event.payload['pipeline_id']} on {event.payload['sha'][:8]}: {_ci_status(event)}"
    return _describe(event).rstrip(".")


def _ci_status(event: InboxEvent) -> str:
    status = _CI_STATUS[event.kind]
    return f"{status} ({event.payload['status']})" if event.kind == EventKind.CI_STOPPED else status


def _not_woken(event: InboxEvent) -> str:
    return f"You were not asked to act on it: {_QUIET_REASONS.get(event.reason, event.reason)}."


def _author(event: InboxEvent) -> str:
    return event.author_username or (event.author.username if event.author is not None else "someone")


def _cut(text: str) -> str:
    return text if len(text) <= QUOTE_MAX_CHARS else f"{text[:QUOTE_MAX_CHARS]}…"


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" for line in _cut(text).splitlines() or [""])
