"""Where one requester's waking requests split into runs: a builtin slash command runs alone, as
``SlashCommandMiddleware`` answers it only when it opens a run's request, and then ends the run."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from codebase.base import Scope
from sessions.inbox.kinds import PERSON_KINDS
from sessions.inbox.render import RunInput
from slash_commands.parser import parse_slash_command
from slash_commands.registry import slash_command_registry

if TYPE_CHECKING:
    import uuid

    from sessions.models import InboxEvent

_MENTIONED = re.compile(r"(?<![\w@])@([\w.-]+)")


class RequestSplits:
    """``asked``, one requester's waking events for a branch oldest first, split into the runs that take them in turn
    (:attr:`splits`): a builtin slash command (:meth:`runs_alone`) alone, and the requests between two commands
    together."""

    def __init__(self, asked: list[InboxEvent]):
        self.splits: list[list[InboxEvent]] = []
        joinable = False
        for event in asked:
            alone = self.runs_alone(event)
            if joinable and not alone:
                self.splits[-1].append(event)
            else:
                self.splits.append([event])
            joinable = not alone

    @staticmethod
    def runs_alone(event: InboxEvent) -> bool:
        """Whether the waking person ``event``'s request, as a run renders it (:meth:`RunInput.request`), is a builtin
        slash command. Inbox runs run in the GLOBAL scope (``run_job_task``).

        The bot's username is not known here, so a command after any mention counts: a wrong match costs only a run of
        its own, while a command that joined other requests would answer for all of them, or be read as prose."""
        if event.kind not in PERSON_KINDS or not event.wake:
            return False
        text = RunInput([event]).request()
        for name in set(_MENTIONED.findall(text)) or {""}:
            command = parse_slash_command(text, name)
            if command is not None and slash_command_registry.get_commands(scope=Scope.GLOBAL, command=command.command):
                return True
        return False

    def holding(self, run_id: uuid.UUID) -> int:
        """The index of the split the next run ``run_id`` serves: the last holding one of its events, else the first.
        Each split before it gets a run of its own."""
        return max(
            (index for index, requests in enumerate(self.splits) if any(e.run_id == run_id for e in requests)),
            default=0,
        )
