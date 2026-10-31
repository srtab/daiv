"""Each inbox event kind's payload, how it ranks when it wakes the agent, and the keys it is stored under.

A payload holds what the renderer and the timeline need, never credentials. The source key de-duplicates an event per
session (``InboxEvent`` is unique on ``(session, source_key)``); the subject groups events of which only the newest
matters when a run takes them.
"""

# No ``from __future__ import annotations``: TypedDict reads ``NotRequired`` only from evaluated annotations.
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

from sessions.models import EventKind, SessionOrigin

if TYPE_CHECKING:
    import uuid

KEY_MAX_LENGTH = 255


class CiPayload(TypedDict):
    pipeline_id: int
    sha: str
    status: str
    web_url: str
    failed_jobs: list[str]


class CommentPayload(TypedDict):
    body: str
    web_url: str
    note_id: NotRequired[int]
    discussion_id: NotRequired[str]
    review_comment_id: NotRequired[int]
    is_diff: NotRequired[bool]


class BranchPushedPayload(TypedDict):
    before: str
    after: str
    commit_count: int
    pusher_username: str


class ForeignPublishPayload(TypedDict):
    thread_id: str
    owner_username: str
    sha: str


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


class ChatMessagePayload(TypedDict):
    text: str
    agui_message_id: str
    agent_model: NotRequired[str]
    agent_thinking_level: NotRequired[str]
    attachments: NotRequired[list[dict[str, Any]]]


class DelegationFinishedPayload(TypedDict):
    batch_id: str
    summary: str


class DelegatedCiOutcomePayload(TypedDict):
    child_thread_id: str
    merge_request_iid: int
    outcome: str
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

#: Waking kinds that mute and the consecutive-run limit may turn into remembered ones (``sessions.inbox.policy``).
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


def validate_payload(kind: str, payload: Any) -> None:
    """Raise ``ValueError`` unless ``payload`` holds every key ``kind`` requires."""
    if kind not in PAYLOADS:
        raise ValueError(f"unknown inbox event kind {kind!r}")
    if not isinstance(payload, Mapping):
        raise ValueError(f"a {kind} payload must be a mapping")
    if missing := sorted(PAYLOADS[kind].__required_keys__ - payload.keys()):  # ty: ignore[unresolved-attribute]
        raise ValueError(f"a {kind} payload is missing {', '.join(missing)}")


def run_trigger_type(kind: str, payload: Mapping[str, Any]) -> str:
    """The trigger type a run started by ``kind`` gets."""
    if kind == EventKind.FOLLOW_UP_JOB:
        trigger = payload.get("trigger_type")
        return trigger if trigger in _JOB_TRIGGERS else SessionOrigin.API_JOB
    if kind not in _TRIGGERS:
        raise ValueError(f"a {kind} event never wakes the agent")
    return _TRIGGERS[kind]


def _key(*parts: object) -> str:
    return ":".join(str(part) for part in parts)[:KEY_MAX_LENGTH]


def pipeline_key(pipeline_id: int) -> str:
    return _key("pipeline", pipeline_id)


def note_key(note_id: int) -> str:
    return _key("note", note_id)


def review_comment_key(comment_id: int) -> str:
    return _key("review-comment", comment_id)


def batch_key(batch_id: uuid.UUID | str) -> str:
    return _key("batch", batch_id)


def chat_key(agui_message_id: str) -> str:
    return _key("chat", agui_message_id)


def job_key(job_id: uuid.UUID | str) -> str:
    return _key("job", job_id)


def push_key(after_sha: str) -> str:
    return _key("push", after_sha)


def mr_state_key(merge_request_iid: int, state: str) -> str:
    return _key("mr-state", merge_request_iid, state)


def child_ci_key(child_thread_id: str, pipeline_id: int) -> str:
    return _key("child-ci", child_thread_id, pipeline_id)


def ci_subject(merge_request_iid: int) -> str:
    return _key("ci", merge_request_iid)


def mr_state_subject(merge_request_iid: int) -> str:
    return _key("mr-state", merge_request_iid)


def branch_subject(ref: str) -> str:
    return _key("branch", ref)


def child_ci_subject(child_thread_id: str, merge_request_iid: int) -> str:
    return _key("child-ci", child_thread_id, merge_request_iid)
