import uuid
from datetime import timedelta
from itertools import count

from django.utils import timezone

from sessions.inbox import kinds
from sessions.models import EventKind, EventState, InboxEvent, Session, SessionOrigin

_clock = count()


def ci_payload(pipeline_id: int = 830724, *, sha: str = "e96f1f77aa", status: str = "failed", jobs=("build",)) -> dict:
    return {
        "pipeline_id": pipeline_id,
        "sha": sha,
        "status": status,
        "web_url": f"https://gitlab.example/pipelines/{pipeline_id}",
        "failed_jobs": list(jobs),
    }


def chat_payload(text: str = "Please also add a test", message_id: str | None = None, **extra) -> dict:
    return {"text": text, "agui_message_id": message_id or f"msg-{uuid.uuid4().hex[:8]}", **extra}


def mention_payload(body: str = "@daiv why is CI red?", note_id: int = 5521) -> dict:
    return {
        "note_id": note_id,
        "discussion_id": "d1",
        "body": body,
        "web_url": f"https://gitlab.example/mr/9#{note_id}",
    }


async def amake_session(**fields) -> Session:
    defaults = {"origin": SessionOrigin.CHAT, "repo_id": "group/repo", "ref": "chore/x"}
    return await Session.objects.acreate(thread_id=str(uuid.uuid4()), **(defaults | fields))


def _event_fields(session: Session, kind: str, fields: dict) -> dict:
    """An event of ``kind`` on ``session``, a moment later than the last one made, with a payload to suit."""
    payloads = {
        EventKind.CI_FAILED: ci_payload,
        EventKind.CI_PASSED: lambda: ci_payload(status="success", jobs=()),
        EventKind.CI_STOPPED: lambda: ci_payload(status="manual", jobs=()),
        EventKind.CHAT_MESSAGE: chat_payload,
        EventKind.MENTION: mention_payload,
        EventKind.COMMENT: mention_payload,
        EventKind.NOTICE: lambda: {"text": "Something happened."},
        EventKind.DELEGATION_FINISHED: lambda: {"batch_id": str(uuid.uuid4()), "summary": "2 jobs finished."},
        EventKind.FOLLOW_UP_JOB: lambda: {"prompt": "Bump the version", "trigger_type": SessionOrigin.API_JOB},
        EventKind.MR_MERGED: lambda: {"target_branch": "main", "by_username": "sfr"},
    }
    payload = fields.pop("payload", None) or payloads[kind]()
    return {
        "session": session,
        "kind": kind,
        "source_key": f"{kind}:{uuid.uuid4().hex}",
        "payload": payload,
        "wake": kind in kinds.WAKING_KINDS,
        "ref": session.ref if kind in kinds.WAKING_KINDS else "",
        "state": EventState.PENDING,
        "created_at": timezone.now() - timedelta(hours=1) + timedelta(seconds=next(_clock)),
        **fields,
    }


async def amake_event(session: Session, kind: str = EventKind.CI_FAILED, **fields) -> InboxEvent:
    return await InboxEvent.objects.acreate(**_event_fields(session, kind, fields))


def make_event(session: Session, kind: str = EventKind.CI_FAILED, **fields) -> InboxEvent:
    """SYNC ONLY."""
    return InboxEvent.objects.create(**_event_fields(session, kind, fields))
