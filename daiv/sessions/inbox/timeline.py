"""The session page's view of the inbox: the events its timeline shows as lines and the ones that open its runs."""

from __future__ import annotations

from django.db.models import Q

from sessions.models import EventState, InboxEvent

_OPENED = (EventState.TAKEN, EventState.HANDLED, EventState.DISMISSED, EventState.FAILED)


class Timeline:
    def __init__(self, session_id: str):
        self.session_id = session_id

    def events(self) -> list[InboxEvent]:
        """The session's events its timeline shows as lines, oldest first: the remembered ones a run took or settled,
        and every failed one. Pending ones show under Up next, and so do those a run that has not started took,
        unless the conversation shows that run (``sessions.transcript.interleave_inbox_events``). SYNC ONLY."""
        return list(
            InboxEvent.objects
            .filter(session_id=self.session_id)
            .filter(Q(wake=False) | Q(state=EventState.FAILED))
            .exclude(state__in=(EventState.PENDING, EventState.SUPERSEDED))
            .select_related("author")
            .order_by("created_at", "id")
        )

    def opening_events(self) -> list[InboxEvent]:
        """The waking events the session's runs took, oldest first, which open those runs in its timeline
        (:meth:`opened`). SYNC ONLY."""
        return list(
            InboxEvent.objects
            .filter(session_id=self.session_id, wake=True)
            .filter(Q(run__isnull=False, state__in=_OPENED) | Q(payload__has_key="tried_runs"))
            .select_related("author")
            .order_by("created_at", "id")
        )

    @staticmethod
    def opened(event: InboxEvent) -> list[str]:
        """The ids of the runs ``event`` opens: each failed run that took it and put it back
        (:meth:`sessions.inbox.complete.FailedAttempt.record`), then the run that holds it now."""
        current = [str(event.run_id)] if event.run_id is not None and event.state in _OPENED else []
        return list(dict.fromkeys([*event.payload.get("tried_runs", []), *current]))
