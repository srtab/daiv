"""Whom a run is for, and who asked for an inbox event."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from sessions.inbox.kinds import PERSON_KINDS

if TYPE_CHECKING:
    from accounts.models import User
    from sessions.models import InboxEvent, Run, Session


class Requester(NamedTuple):
    """Whom a run is for: a DAIV user, else the git platform account of a person who never linked one."""

    user_id: int | None
    provider: str = ""
    uid: str = ""

    @classmethod
    def of_run(cls, run: Run) -> Requester:
        return cls(run.user_id, run.external_provider, run.external_uid)

    @classmethod
    def of_event(cls, event: InboxEvent, session: Session) -> Requester:
        """Who asked for ``event``: its author, else a person's unlinked account, else (DAIV woke on its own) the
        session's owner."""
        if event.author_id is not None:
            return cls(event.author_id)
        if event.kind in PERSON_KINDS:
            return cls(None, event.author_provider, event.author_uid)
        return cls(session.user_id)

    def run_lookup(self) -> dict[str, int | str | None]:
        """The ``Run`` fields that identify this requester."""
        return {"user_id": self.user_id, "external_provider": self.provider, "external_uid": self.uid}

    def run_fields(self, username: str = "") -> dict[str, int | str | None]:
        """:meth:`run_lookup` plus ``username``, which names an unlinked account."""
        return self.run_lookup() | {"external_username": "" if self.user_id is not None else username}

    def user(self, session: Session, asked: list[InboxEvent]) -> User | None:
        """The DAIV user this requester names: the author of one of ``asked``, else the session's owner."""
        if self.user_id is None:
            return None
        return next((event.author for event in asked if event.author_id == self.user_id), session.user)
