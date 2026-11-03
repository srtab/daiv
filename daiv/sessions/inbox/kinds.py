"""Each inbox event kind's payload, how it ranks when it wakes the agent, and the keys it is stored under.

A payload holds what the renderer and the timeline need, never credentials. The source key de-duplicates an event per
session (``InboxEvent`` is unique on ``(session, source_key)``); the subject groups events of which only the newest
matters when a run takes them.
"""

# No ``from __future__ import annotations``: TypedDict reads ``NotRequired`` only from evaluated annotations.
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from sessions.models import EventKind, SessionOrigin

if TYPE_CHECKING:
    import uuid

    from sessions.models import InboxEvent

KEY_MAX_LENGTH = 255


class CiPayload(TypedDict):
    pipeline_id: int
    sha: str
    status: str
    web_url: str
    failed_jobs: list[str]
    workflow: NotRequired[str]
    attempts: NotRequired[int]


class CommentPayload(TypedDict):
    body: str
    web_url: str
    note_id: NotRequired[int]
    discussion_id: NotRequired[str]
    review_comment_id: NotRequired[int]
    is_diff: NotRequired[bool]
    agent_model: NotRequired[str]
    agent_thinking_level: NotRequired[str]


class BranchPushedPayload(TypedDict):
    before: str
    after: str
    commit_count: int
    pusher_username: str


class ForeignPublishPayload(TypedDict):
    thread_id: str
    owner_username: str
    sha: str
    title: NotRequired[str]
    web_url: NotRequired[str]


class MergeRequestStatePayload(TypedDict):
    target_branch: str
    by_username: str


class FollowUpJobPayload(TypedDict):
    prompt: str
    trigger_type: str
    ref: NotRequired[str]
    agent_model: NotRequired[str]
    agent_thinking_level: NotRequired[str]
    references: NotRequired[list[dict[str, Any]]]
    muted: NotRequired[bool]
    sandbox_environment_id: NotRequired[str]


class ChatMessagePayload(TypedDict):
    text: str
    agui_message_id: str
    agent_model: NotRequired[str]
    agent_thinking_level: NotRequired[str]
    attachments: NotRequired[list[dict[str, Any]]]
    mcp_overrides: NotRequired[dict[str, Any]]


class DelegationFinishedPayload(TypedDict):
    batch_id: str
    summary: str
    succeeded: NotRequired[int]
    total: NotRequired[int]
    muted: NotRequired[bool]


DelegatedOutcome = Literal["passing", "stopped_after_attempts", "no_push"]


class DelegatedCiOutcomePayload(TypedDict):
    child_thread_id: str
    merge_request_iid: int
    outcome: DelegatedOutcome
    web_url: NotRequired[str]


class NoticePayload(TypedDict):
    text: str


PAYLOADS: dict[str, type] = {
    EventKind.CHAT_MESSAGE: ChatMessagePayload,
    EventKind.MENTION: CommentPayload,
    EventKind.FOLLOW_UP_JOB: FollowUpJobPayload,
    EventKind.DELEGATION_FINISHED: DelegationFinishedPayload,
    EventKind.CI_FAILED: CiPayload,
    EventKind.CI_PASSED: CiPayload,
    EventKind.CI_STOPPED: CiPayload,
    EventKind.BRANCH_PUSHED: BranchPushedPayload,
    EventKind.FOREIGN_PUBLISH: ForeignPublishPayload,
    EventKind.MR_MERGED: MergeRequestStatePayload,
    EventKind.MR_CLOSED: MergeRequestStatePayload,
    EventKind.COMMENT: CommentPayload,
    EventKind.DELEGATED_CI_OUTCOME: DelegatedCiOutcomePayload,
    EventKind.NOTICE: NoticePayload,
}

#: Waking kinds, highest precedence first: the run that takes several gets the trigger type of the first (R6).
PRECEDENCE: dict[str, int] = {
    EventKind.CHAT_MESSAGE: 1,
    EventKind.MENTION: 2,
    EventKind.FOLLOW_UP_JOB: 3,
    EventKind.DELEGATION_FINISHED: 4,
    EventKind.CI_FAILED: 5,
}
WAKING_KINDS = frozenset(PRECEDENCE)

#: Waking kinds that the consecutive-run limit may turn into remembered ones (``sessions.inbox.policy``).
GATED_KINDS = frozenset({EventKind.CI_FAILED, EventKind.DELEGATION_FINISHED})

#: Waking kinds a person asked for; their words are the run's request, and their author may start it.
PERSON_KINDS = frozenset({EventKind.CHAT_MESSAGE, EventKind.MENTION, EventKind.FOLLOW_UP_JOB})

_TRIGGERS: dict[str, str] = {
    EventKind.CHAT_MESSAGE: SessionOrigin.CHAT,
    EventKind.MENTION: SessionOrigin.MR_WEBHOOK,
    EventKind.DELEGATION_FINISHED: SessionOrigin.DELEGATION_RESUME,
    EventKind.CI_FAILED: SessionOrigin.PIPELINE_WEBHOOK,
}
_JOB_TRIGGERS = frozenset({SessionOrigin.API_JOB, SessionOrigin.MCP_JOB})

#: Every trigger type an inbox run can get, which is what a session's QUEUED next run is created with.
INBOX_TRIGGERS = frozenset(_TRIGGERS.values()) | _JOB_TRIGGERS


@dataclass(frozen=True)
class EventContent:
    """An inbox event's kind and payload."""

    kind: str
    payload: Mapping[str, Any]

    @classmethod
    def of(cls, event: InboxEvent) -> EventContent:
        return cls(event.kind, event.payload)

    def validate_payload(self) -> None:
        """Raise ``ValueError`` unless the payload holds every key the kind requires."""
        if self.kind not in PAYLOADS:
            raise ValueError(f"unknown inbox event kind {self.kind!r}")
        if not isinstance(self.payload, Mapping):
            raise ValueError(f"a {self.kind} payload must be a mapping")
        if missing := sorted(PAYLOADS[self.kind].__required_keys__ - self.payload.keys()):  # ty: ignore[unresolved-attribute]
            raise ValueError(f"a {self.kind} payload is missing {', '.join(missing)}")

    def follows_session(self) -> bool:
        """Whether a waking event concerns whichever branch its session is on, not a branch of its own: a chat message,
        a follow-up job sent without a branch, a finished delegated batch."""
        if self.kind == EventKind.FOLLOW_UP_JOB:
            return not self.payload.get("ref")
        return self.kind in (EventKind.CHAT_MESSAGE, EventKind.DELEGATION_FINISHED)

    def run_trigger_type(self) -> str:
        """The trigger type a run started by this kind gets."""
        if self.kind == EventKind.FOLLOW_UP_JOB:
            trigger = self.payload.get("trigger_type")
            return trigger if trigger in _JOB_TRIGGERS else SessionOrigin.API_JOB
        if self.kind not in _TRIGGERS:
            raise ValueError(f"a {self.kind} event never wakes the agent")
        return _TRIGGERS[self.kind]


def _join(*parts: object) -> str:
    return ":".join(str(part) for part in parts)[:KEY_MAX_LENGTH]


class SourceKey:
    """The keys an event is de-duplicated under, per session."""

    @staticmethod
    def pipeline(pipeline_id: int, kind: str = EventKind.CI_FAILED, reason: str | None = None) -> str:
        """A waking failure keys on the pipeline alone; a quiet one, a pass and a stop each on a suffix of their own, so
        a retried pipeline's new result is not taken for one already posted."""
        if reason:
            return _join("pipeline", pipeline_id, reason)
        if kind == EventKind.CI_FAILED:
            return _join("pipeline", pipeline_id)
        return _join("pipeline", pipeline_id, kind)

    @staticmethod
    def note(note_id: int) -> str:
        return _join("note", note_id)

    @staticmethod
    def review_comment(comment_id: int) -> str:
        return _join("review-comment", comment_id)

    @staticmethod
    def comment(note_or_review_comment_key: str) -> str:
        return _join("comment", note_or_review_comment_key)

    @staticmethod
    def batch(batch_id: uuid.UUID | str) -> str:
        return _join("batch", batch_id)

    @staticmethod
    def chat(agui_message_id: str) -> str:
        return _join("chat", agui_message_id)

    @staticmethod
    def job(job_id: uuid.UUID | str) -> str:
        return _join("job", job_id)

    @staticmethod
    def push(after_sha: str) -> str:
        return _join("push", after_sha)

    @staticmethod
    def mr_state(merge_request_iid: int, state: str) -> str:
        return _join("mr-state", merge_request_iid, state)

    @staticmethod
    def child_ci(child_thread_id: str, pipeline_id: int, outcome: str) -> str:
        """Keyed on the outcome too: a retried pipeline keeps its id, so a pass after ``no_push`` is a new ending."""
        return _join("child-ci", child_thread_id, pipeline_id, outcome)


class Subject:
    """The groups of events of which only the newest matters when a run takes them."""

    @staticmethod
    def ci(merge_request_iid: int, workflow_id: int | None = None) -> str:
        """A merge request's CI, or one workflow's on it: GitHub runs a workflow run per workflow on a commit, so one
        workflow's newer run must not replace another's."""
        if workflow_id is None:
            return _join("ci", merge_request_iid)
        return _join("ci", merge_request_iid, workflow_id)

    @staticmethod
    def mr_state(merge_request_iid: int) -> str:
        return _join("mr-state", merge_request_iid)

    @staticmethod
    def branch(ref: str) -> str:
        return _join("branch", ref)

    @staticmethod
    def child_ci(child_thread_id: str, merge_request_iid: int) -> str:
        return _join("child-ci", child_thread_id, merge_request_iid)
