"""The skill page's Usage tab: how often the skill ran, and the runs that invoked it most recently."""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.urls import reverse

from sessions.models import Run, Session

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from skills.models import SkillInvocation

RECENT_RUNS = 20


@dataclass(frozen=True)
class SkillRun:
    """One invocation and the run it happened in; ``run`` is ``None`` when no run of the session matches it."""

    invocation: SkillInvocation
    session: Session | None
    run: Run | None

    @property
    def url(self) -> str:
        """The run in its session's transcript, else the session; empty once the session is gone."""
        if self.session is None:
            return ""
        url = reverse("session_detail", kwargs={"thread_id": self.session.thread_id})
        return f"{url}#run-{self.run.pk}" if self.run is not None else url

    @property
    def title(self) -> str:
        return (self.run.title if self.run else "") or (self.session.title if self.session else "")

    @property
    def repo_slug(self) -> str:
        return self.session.repo_id if self.session else self.invocation.repo_slug


def match_runs(invocations: list[SkillInvocation]) -> list[SkillRun]:
    """Each invocation with the run it happened in: the latest run of its session that started before it.

    An invocation records only its session, and a session's runs never overlap, so the start times place it.
    """
    if not invocations:
        return []
    # A session's thread id is stored hyphenated or as bare hex, while an invocation holds it as a UUID.
    thread_uuids = {
        text: invocation.thread_id
        for invocation in invocations
        for text in (str(invocation.thread_id), invocation.thread_id.hex)
    }
    sessions = {thread_uuids[s.thread_id]: s for s in Session.objects.filter(thread_id__in=thread_uuids)}
    starts: dict[UUID, list[datetime]] = defaultdict(list)
    runs: dict[UUID, list[Run]] = defaultdict(list)
    latest = max(invocation.created for invocation in invocations)
    session_runs = (
        Run.objects
        .filter(
            session_id__in=[session.thread_id for session in sessions.values()],
            started_at__isnull=False,
            started_at__lte=latest,
        )
        .only("session_id", "started_at", "status", "trigger_type", "title", "agent_model")
        .order_by("started_at")
    )
    for run in session_runs:
        starts[thread_uuids[run.session_id]].append(run.started_at)
        runs[thread_uuids[run.session_id]].append(run)

    matched = []
    for invocation in invocations:
        index = bisect.bisect_right(starts[invocation.thread_id], invocation.created) - 1
        run = runs[invocation.thread_id][index] if index >= 0 else None
        matched.append(SkillRun(invocation, sessions.get(invocation.thread_id), run))
    return matched
