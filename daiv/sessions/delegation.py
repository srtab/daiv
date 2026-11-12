"""Delegated jobs: a coordinator run fans tailored sub-jobs (legs) out with ``delegate_jobs``, to other repositories or
to new sessions on its own, ends its turn, and is resumed once every leg has finished.

:class:`SessionJobDelegator` is the agent's ``automation.agent.delegation.JobDelegator`` for executor runs, and
:meth:`SessionJobDelegator.afor` hands one only to a session that may delegate. It submits the new legs as one batch of
``SessionOrigin.DELEGATED_JOB`` runs as the user who triggered the delegating run, muted when that run is, each in a
new session whose ``parent`` is the coordinator's. Delegation is one level deep: a session with a parent gets no
delegator. Failing legs cannot loop a coordinator forever: the inbox stops waking a session after
``sessions.inbox.policy.MAX_SYSTEM_RUNS_IN_A_ROW`` runs in a row that nobody asked for.

A new leg always publishes to a new branch and merge request of its own (``run_job_task``'s ``fork_branch``), so any
number of legs may share a base, the coordinator's own included. A leg that names the coordinator's own branch
``waits_for`` the delegating run: it stays QUEUED until that run has finished, and so has published what it changed
this turn, and the queue starts it then (``sessions.queue.release_runs_waiting_for``), from the branch the
coordinator's session works on by then, which differs when the coordinator published to a new branch, as a run on
the default branch always does.

A target that names a leg's ``thread_id`` is a leg follow-up: it continues that leg in its own session, with its
conversation, on its branch and merge request (:mod:`sessions.inbox.leg_follow_ups`). It is refused, with a reason the
coordinator can act on and the call's other targets going ahead, unless it names a leg this coordinator delegated, no
``ref``, the leg's repository, a leg whose merge request is still open, not archived for its user, whose delegated work
has all ended, and that DAIV did not lose track of (:data:`_REFUSALS`, checked in that order), then on a repository
its user may still run on, checked with the new legs' and again by the inbox when its run starts. A leg follow-up's run
carries the call's ``batch_id``, so one batch may hold new legs' first runs and leg follow-ups' runs, and a leg session
serves every batch that sent it work. The call validates everything first, posts the leg follow-ups without draining,
submits the new legs, then drains the followed-up legs' sessions, so none of the batch's runs starts before every run of
it exists. The per-minute sweep can still drain a leg between its post and that submission: a follow-up whose merge
request was merged or closed, or whose access was revoked, in that moment finishes at once, and its batch resumes the
coordinator without the new legs.

:meth:`DelegatedBatch.resume_after` runs on every leg's ``run_finished``. Once the whole batch is terminal it posts
one waking ``delegation_finished`` event to the coordinator's inbox (source key ``batch:<id>``, so several legs
finishing at once post it once), whose summary covers every leg, from its full reply up to ``LEG_REPLY_MAX_CHARS``,
without quoting DAIV's own error text. The inbox starts the coordinator's ``SessionOrigin.DELEGATION_RESUME`` run
once the session is free, as the legs' user and with their mute, after checking that user may still run on every
repository of its session. A batch none of whose runs reached the task queue, waited for the coordinator or was a leg
follow-up's resumes nothing: the tool already told the agent nothing started, and neither does one whose every run
was cancelled. A leg follow-up's run counts because the inbox starts it: one refused access, or not run since its
leg's merge request was merged or closed, never reaches the queue and still ends its batch.
``continuation_of_batch_id`` is no longer written; it stays readable on older continuations.

``SessionJobDelegator.astatus`` reports the session's batches, its latest ``MAX_STATUS_BATCHES`` without a batch id,
and ``acancel`` asks a batch's unfinished legs to stop through ``RunManager.cancel``: a leg that is not running yet
finishes FAILED at once, and a running one is ``stopping`` until its ``run_job_task`` stops at its next check and it
finishes as that task does. A waiting leg follow-up is withdrawn the same way, and only its own inbox event goes with
it. Neither quotes DAIV's own error text, as the continuation's prompt does not.

:meth:`DelegatedBatch.resume_stalled` is the periodic backstop: it posts the event for a finished batch the signal
path left without one, and releases the legs and older continuations left QUEUED on a session with no active run once
they are not waiting. Retrying a resume that failed is the inbox's own job.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from functools import cached_property
from typing import TYPE_CHECKING

from django.db.models import Count, Max, Q
from django.urls import reverse
from django.utils import timezone

from asgiref.sync import sync_to_async
from sandbox_envs.selection import aresolve_repo_envs

from automation.agent.delegation import (
    MAX_DELEGATED_TARGETS,
    BatchStatus,
    CancelResult,
    DelegationError,
    DelegationResult,
    FailedLeg,
    LegStatus,
    StartedLeg,
)
from codebase.authorization import REPO_ACCESS_DENIED_MESSAGE
from core.utils import build_absolute_url
from sessions.admission import AdmissionError, RepoAccessError, aassert_repo_access, check_agent_override
from sessions.artifacts import aresolve_active_run
from sessions.inbox import kinds
from sessions.inbox.drain import Drain
from sessions.inbox.leg_follow_ups import BUSY, LegFollowUpRefused, LegFollowUps
from sessions.inbox.post import Inbox
from sessions.legs import NOT_RUN_CLOSED, NOT_RUN_MERGED, Leg
from sessions.models import (
    LINK_FAILED_PREFIX,
    EventKind,
    InboxEvent,
    MergeRequestState,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)
from sessions.queue import release_orphan_queued_runs
from sessions.services import RepoTarget, asubmit_batch_runs

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet

    from accounts.models import User
    from automation.agent.delegation import Checkout, DelegatedTarget
    from sessions.services import BatchSubmitFailure

logger = logging.getLogger("daiv.sessions")

LEG_REPLY_MAX_CHARS = 4000
LEG_OVERVIEW_REPLY_MAX_CHARS = 1000
MAX_STATUS_BATCHES = 5
STALLED_BATCH_WINDOW = timedelta(days=1)

_UNSTARTED_LEG = "DAIV could not start this leg; the error is on DAIV's side, not in the request."
_UNTRACKED_LEG = (
    "This leg started, but DAIV lost track of it: it may still open a merge request, and its outcome will not "
    "reach you."
)
_NOT_A_LEG = (
    "`{thread_id}` is not a leg you delegated: give a `thread_id` from a resume or `check_delegated_jobs`, or delegate "
    "a new leg."
)
_REFUSALS = {
    "ref": "A leg follow-up works on the leg's own branch: omit `ref`.",
    "repo": "This leg works on `{leg_repo_id}`: give that `repo_id`, or delegate a new leg for `{repo_id}`.",
    "archived": (
        "The leg's session is archived for the person you act for: they can unarchive it from its session page, or "
        "you can delegate a new leg."
    ),
    BUSY: "This leg is still working: you are resumed when its batch finishes, and can follow it up then.",
    "lost": (
        "DAIV lost track of this leg's last run, which may still be working: check its session (`{session_url}`) "
        "before sending it more work, or delegate a new leg."
    ),
    MergeRequestState.MERGED: "This leg's merge request was merged: delegate a new leg for further work.",
    MergeRequestState.CLOSED: "This leg's merge request was closed: delegate a new leg for further work.",
}
_CUT_MARKER = "[cut at {limit:,} characters: the rest is in the leg's session, {session_url}]"


class SessionJobDelegator:
    """Submits ``delegate_jobs`` legs for the run on ``thread_id``, as the user who triggered it."""

    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id

    @classmethod
    async def afor(cls, thread_id: str) -> SessionJobDelegator | None:
        """The delegator for a run on ``thread_id``; ``None`` for a delegated session, which cannot delegate further."""
        if await Session.objects.filter(pk=thread_id, parent__isnull=False).aexists():
            return None
        return cls(thread_id)

    @staticmethod
    def check(goal: str, targets: Sequence[DelegatedTarget]) -> None:
        """Raise :class:`DelegationError` for a ``delegate_jobs`` call without a goal or with targets it cannot start:
        the target limit counts new legs and leg follow-ups together, and a leg named twice refuses the whole call."""
        if not goal:
            raise DelegationError("`goal` must name the overall objective.")
        if not targets:
            raise DelegationError("Give at least one target.")
        if len(targets) > MAX_DELEGATED_TARGETS:
            raise DelegationError(f"At most {MAX_DELEGATED_TARGETS} targets per call; split the work across calls.")
        if any(not target.repo_id or not target.prompt for target in targets):
            raise DelegationError("Every target needs a `repo_id` and a prompt of its own.")
        named = [target.thread_id for target in targets if target.thread_id]
        if twice := next((thread_id for thread_id in named if named.count(thread_id) > 1), None):
            raise DelegationError(f"Name each leg once per call: {twice} appears more than once.")

    async def adelegate(self, *, checkout: Checkout, goal: str, targets: Sequence[DelegatedTarget]) -> DelegationResult:
        run = await self._aactive_run()
        if run is None:
            raise DelegationError("This run has no session to resume, so it cannot delegate.")
        if run.user is None:
            raise DelegationError(
                "Sub-jobs run as the DAIV user who triggered this run, and it has none, so it cannot delegate."
            )
        self.check(goal, targets)

        user = run.user
        outcomes: dict[int, StartedLeg | FailedLeg] = {}
        legs: dict[int, Leg] = {}
        for index, target in enumerate(targets):
            if target.thread_id:
                leg, refusal = await sync_to_async(self._check_follow_up)(target, user)
                if refusal is not None:
                    outcomes[index] = FailedLeg(target.repo_id, target.ref, refusal, thread_id=target.thread_id)
                else:
                    legs[index] = leg
        new_legs = [index for index, target in enumerate(targets) if not target.thread_id]
        repo_of = {index: targets[index].repo_id for index in new_legs} | {
            index: leg.session.repo_id for index, leg in legs.items()
        }
        denied: set[str] = set()
        if repo_of:
            try:
                await aassert_repo_access(user, list(repo_of.values()))
            except RepoAccessError as err:
                denied = set(err.repo_ids)
        for index, repo_id in repo_of.items():
            if repo_id in denied:
                target = targets[index]
                outcomes[index] = FailedLeg(
                    target.repo_id, target.ref, REPO_ACCESS_DENIED_MESSAGE, thread_id=target.thread_id
                )
                legs.pop(index, None)
        new_legs = [index for index in new_legs if index not in outcomes]
        if not new_legs and not legs:
            return DelegationResult(batch_id=None, failed=[outcomes[index] for index in sorted(outcomes)])

        batch_id = uuid.uuid4()
        try:
            check_agent_override(None, None)
            repos = (
                await aresolve_repo_envs(
                    user=user,
                    repos=[
                        RepoTarget(
                            repo_id=targets[index].repo_id,
                            ref=targets[index].ref,
                            prompt=targets[index].prompt,
                            references=targets[index].references,
                            waits_for_run_id=run.pk if self._on_own_branch(checkout, targets[index]) else None,
                        )
                        for index in new_legs
                    ],
                    explicit_env_id=None,
                )
                if new_legs
                else []
            )
        except AdmissionError as err:
            raise DelegationError(str(err)) from err

        posted: list[str] = []
        for index, leg in legs.items():
            outcomes[index] = await self._apost_follow_up(
                targets[index], leg, batch_id=batch_id, user=user, muted=run.effective_muted
            )
            if isinstance(outcomes[index], StartedLeg):
                posted.append(leg.session.pk)

        if repos:
            reason = None
            try:
                result = await asubmit_batch_runs(
                    user=user,
                    prompt=goal,
                    repos=repos,
                    trigger_type=SessionOrigin.DELEGATED_JOB,
                    muted=run.effective_muted,
                    parent=run.session,
                    batch_id=batch_id,
                )
            except AdmissionError as err:
                if not posted:
                    raise DelegationError(str(err)) from err
                reason = str(err)
            except Exception:
                if not posted:
                    raise
                logger.exception("delegate_jobs: the new legs of batch %s could not be submitted", batch_id)
                reason = _UNSTARTED_LEG
            else:
                started = await sync_to_async(self._started_legs)(result.outcomes)
                outcomes.update(zip(new_legs, started, strict=True))
            if reason is not None:
                outcomes.update({
                    index: FailedLeg(targets[index].repo_id, targets[index].ref, reason) for index in new_legs
                })

        for thread_id in posted:
            await sync_to_async(Drain(thread_id).start_logged)()

        ordered = [outcomes[index] for index in sorted(outcomes)]
        return DelegationResult(
            batch_id=str(batch_id) if posted or repos else None,
            started=[outcome for outcome in ordered if isinstance(outcome, StartedLeg)],
            failed=[outcome for outcome in ordered if isinstance(outcome, FailedLeg)],
        )

    def _check_follow_up(self, target: DelegatedTarget, user: User) -> tuple[Leg | None, str | None]:
        """The leg ``target`` follows up, and why it is refused, or ``None`` when it may be sent; the first failing
        check gives the reason. SYNC ONLY."""
        leg = Leg.of(target.thread_id, coordinator=self._thread_id)
        if leg is None:
            return None, _NOT_A_LEG.format(thread_id=target.thread_id)
        session = leg.session
        if target.ref:
            return leg, _REFUSALS["ref"]
        if target.repo_id.casefold() != session.repo_id.casefold():
            return leg, _REFUSALS["repo"].format(leg_repo_id=session.repo_id, repo_id=target.repo_id)
        if (state := leg.merge_request_state()) != MergeRequestState.OPEN:
            return leg, _REFUSALS[state]
        if leg.archived_by(user):
            return leg, _REFUSALS["archived"]
        if leg.is_busy():
            return leg, _REFUSALS[BUSY]
        if leg.is_lost():
            return leg, _REFUSALS["lost"].format(session_url=LegRun.url_of(session.pk))
        return leg, None

    async def _apost_follow_up(
        self, target: DelegatedTarget, leg: Leg, *, batch_id: uuid.UUID, user: User, muted: bool
    ) -> StartedLeg | FailedLeg:
        """Post ``target``'s leg follow-up to ``leg`` in ``batch_id``, without draining its session."""
        try:
            follow_up = await LegFollowUps(leg).apost(
                prompt=target.prompt,
                batch_id=batch_id,
                user=user,
                muted=muted,
                references=[ref.to_stored() for ref in target.references],
            )
        except LegFollowUpRefused as refused:
            return FailedLeg(target.repo_id, target.ref, _REFUSALS[refused.reason], thread_id=target.thread_id)
        except Exception:
            logger.exception("delegate_jobs: could not post a leg follow-up to thread_id=%s", target.thread_id)
            return FailedLeg(target.repo_id, target.ref, _UNSTARTED_LEG, thread_id=target.thread_id)
        return StartedLeg(
            follow_up.repo_id,
            follow_up.ref,
            str(follow_up.session_id),
            await sync_to_async(LegRun.url_of)(follow_up.session_id),
            leg_follow_up=True,
        )

    async def astatus(self, *, batch_id: str | None) -> list[BatchStatus]:
        return await sync_to_async(self._status)(batch_id)

    async def acancel(self, *, batch_id: str, thread_ids: Sequence[str]) -> CancelResult:
        return await sync_to_async(self._cancel)(batch_id, set(thread_ids))

    def _status(self, batch_id: str | None) -> list[BatchStatus]:
        if batch_id:
            return [DelegatedBatch(self._batch_legs(batch_id)).status(reply_limit=LEG_REPLY_MAX_CHARS)]
        latest = list(
            self
            ._legs()
            .values("batch_id")
            .annotate(latest=Max("created_at"))
            .order_by("-latest")
            .values_list("batch_id", flat=True)[:MAX_STATUS_BATCHES]
        )
        by_batch = DelegatedBatch.group(self._legs().filter(batch_id__in=latest).with_leg_follow_up())
        batches = [DelegatedBatch(by_batch[batch]) for batch in latest]
        return DelegatedBatch.statuses(batches, reply_limit=LEG_OVERVIEW_REPLY_MAX_CHARS)

    def _cancel(self, batch_id: str, thread_ids: set[str]) -> CancelResult:
        legs = self._batch_legs(batch_id)
        if unknown := thread_ids - {leg.session_id for leg in legs}:
            raise DelegationError(f"Not legs of batch {batch_id}: {', '.join(sorted(unknown))}.")
        cancelled = [
            leg.session_id
            for leg in legs
            if (not thread_ids or leg.session_id in thread_ids)
            and leg.status not in RunStatus.terminal()
            and Run.objects.cancel(leg)
        ]
        batch = DelegatedBatch(legs)
        return CancelResult(
            batch=batch.status(reply_limit=LEG_REPLY_MAX_CHARS), cancelled=cancelled, resumes=batch.resumes()
        )

    def _legs(self) -> QuerySet[Run]:
        return Run.objects.delegated_by(self._thread_id).select_related("session")

    def _batch_legs(self, batch_id: str) -> list[Run]:
        try:
            batch = uuid.UUID(batch_id)
        except ValueError:
            raise DelegationError(f"`{batch_id}` is not a batch id: give one delegate_jobs returned.") from None
        runs = self._legs().select_related("task_result").filter(batch_id=batch).with_leg_follow_up()
        if legs := list(runs.order_by("created_at", "id")):
            return legs
        raise DelegationError(f"This session delegated no batch {batch_id}.")

    async def _aactive_run(self) -> Run | None:
        if (run := await aresolve_active_run(self._thread_id)) is None:
            return None
        return await Run.objects.select_related("session__scheduled_job", "user").aget(pk=run.pk)

    @staticmethod
    def _on_own_branch(own: Checkout, target: DelegatedTarget) -> bool:
        """Whether ``target`` names the delegating run's own branch, to build on what that run publishes when it
        ends."""
        return target.repo_id.casefold() == own.repo_id.casefold() and bool(target.ref) and target.ref == own.ref

    @staticmethod
    def _started_legs(outcomes: Sequence[Run | BatchSubmitFailure]) -> list[StartedLeg | FailedLeg]:
        """Each new leg's submission outcome as the agent reads it, in order. SYNC ONLY."""
        return [
            StartedLeg(
                outcome.repo_id,
                outcome.ref,
                str(outcome.session_id),
                LegRun.url_of(outcome.session_id),
                starts_after_this_turn=outcome.waits_for_id is not None,
            )
            if isinstance(outcome, Run)
            else SessionJobDelegator._failed_leg(outcome)
            for outcome in outcomes
        ]

    @staticmethod
    def _failed_leg(failure: BatchSubmitFailure) -> FailedLeg:
        """``failure`` for the agent, which may quote it publicly: its error stays in the logs and on the run."""
        if failure.task_enqueued:
            return FailedLeg(failure.repo_id, failure.ref, _UNTRACKED_LEG, still_runs=True)
        return FailedLeg(failure.repo_id, failure.ref, _UNSTARTED_LEG)


# The word the coordinator reads for each state; "queued" is listed before "waits", which reads the same.
_OUTCOMES = {
    "queued": "queued",
    "waits": "queued",
    "running": "running",
    "stopping": "stopping",
    "waiting_input": "stopped to ask a question",
    "finished": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "lost": "started, but DAIV lost track of it",
    "not_run_merged": "not run: its merge request was merged",
    "not_run_closed": "not run: its merge request was closed",
}
_NOT_RUN = {NOT_RUN_MERGED: "not_run_merged", NOT_RUN_CLOSED: "not_run_closed"}
_NOT_RUN_STATES = frozenset(_NOT_RUN.values())

_LEG_STATES = {
    RunStatus.RUNNING: "running",
    RunStatus.WAITING_INPUT: "waiting_input",
    RunStatus.SUCCESSFUL: "finished",
    RunStatus.FAILED: "failed",
}


class LegRun:
    """One delegated run, a new leg's first run or a leg follow-up's, as the coordinator, the transcript and the
    session header read it."""

    def __init__(self, run: Run):
        self.run = run

    @staticmethod
    def url_of(thread_id: str) -> str:
        """The absolute URL of the session page of ``thread_id``."""
        return build_absolute_url(reverse("session_detail", kwargs={"thread_id": thread_id}))

    @staticmethod
    def outcome_states() -> dict[str, str]:
        """The state each word of a jobs tool's leg ``status`` stands for, so the transcript can label the word."""
        states: dict[str, str] = {}
        for state, word in _OUTCOMES.items():
            states.setdefault(word, state)
        return states

    @property
    def state(self) -> str:
        """The status of the delegated job, which the transcript shows and ``_OUTCOMES`` words for the
        coordinator."""
        run = self.run
        if self.stopping:
            return "stopping"
        if run.is_cancelled:
            return "cancelled"
        if run.status == RunStatus.FAILED and run.error_message.startswith(LINK_FAILED_PREFIX):
            return "lost"
        if run.status == RunStatus.FAILED and run.error_message in _NOT_RUN:
            return _NOT_RUN[run.error_message]
        if run.status == RunStatus.QUEUED and run.waits_for_id:
            return "waits"
        return _LEG_STATES.get(run.status, "queued")

    @property
    def stopping(self) -> bool:
        """Whether the run was asked to stop and has not finished yet."""
        return self.run.cancel_requested_at is not None and self.run.status not in RunStatus.terminal()

    @property
    def is_leg_follow_up(self) -> bool:
        """Whether the run is a leg follow-up's, read from a query ``with_leg_follow_up``."""
        return getattr(self.run, "is_leg_follow_up", False)

    def status(self, leg: Leg, *, reply_limit: int) -> LegStatus:
        """The run of ``leg`` as its coordinator sees it: where the leg started, its own branch and merge request even
        when this run published nothing, and this run's reply, cut at ``reply_limit``. SYNC ONLY."""
        run = self.run
        session_url = self.url_of(run.session_id)
        merge_request = leg.merge_request()
        return LegStatus(
            repo_id=run.repo_id,
            ref=leg.base_ref,
            thread_id=run.session_id,
            session_url=session_url,
            status=_OUTCOMES[self.state],
            branch=run.session.ref if leg.has_own_branch() else "",
            merge_request_url=(merge_request.web_url if merge_request is not None else "") or run.merge_request_web_url,
            reply=self._reply(reply_limit, session_url),
            leg_follow_up=self.is_leg_follow_up,
        )

    def payload(self, visible: frozenset[str]) -> dict[str, str | bool]:
        """The run as the transcript and the session header show it; one whose session is not in ``visible`` keeps its
        repository, ref, state and whether it is a leg follow-up, and nothing else."""
        run = self.run
        seen = run.session_id in visible
        return {
            "repo_id": run.repo_id,
            "ref": run.ref,
            "state": self.state,
            "merge_request_url": run.merge_request_web_url if seen else "",
            "session_url": reverse("session_detail", kwargs={"thread_id": run.session_id}) if seen else "",
            "thread_id": run.session_id if seen else "",
            "leg_follow_up": self.is_leg_follow_up,
        }

    def _reply(self, limit: int, session_url: str) -> str:
        reply, cut = self.run.response_text_within(limit)
        if not cut:
            return reply
        return f"{reply}\n{_CUT_MARKER.format(limit=len(reply), session_url=session_url)}"


class DelegatedBatch:
    """The runs one ``delegate_jobs`` call sent, sharing a ``batch_id``, oldest first: new legs' first runs and leg
    follow-ups' runs."""

    def __init__(self, runs: Sequence[Run]):
        self.runs = runs
        self.batch_id = runs[0].batch_id

    @staticmethod
    def group(runs: QuerySet[Run]) -> defaultdict[uuid.UUID, list[Run]]:
        """``runs`` by batch, each oldest first."""
        by_batch: defaultdict[uuid.UUID, list[Run]] = defaultdict(list)
        for run in runs.order_by("created_at", "id"):
            by_batch[run.batch_id].append(run)
        return by_batch

    @staticmethod
    def statuses(batches: Sequence[DelegatedBatch], *, reply_limit: int) -> list[BatchStatus]:
        """Each of ``batches``' status, the legs of all of them loaded at once (:meth:`Leg.many`). SYNC ONLY."""
        leg_of = Leg.many(run.session for batch in batches for run in batch.runs)
        return [
            BatchStatus(
                batch_id=str(batch.batch_id),
                finished=all(run.status in RunStatus.terminal() for run in batch.runs),
                legs=[LegRun(run).status(leg_of[run.session_id], reply_limit=reply_limit) for run in batch.runs],
            )
            for batch in batches
        ]

    def status(self, *, reply_limit: int) -> BatchStatus:
        return self.statuses([self], reply_limit=reply_limit)[0]

    def resumes(self) -> bool:
        """Whether the batch resumes its coordinator once it has finished: some run reached the task queue, waited for
        the coordinator or is a leg follow-up's, which the inbox starts, and not every run was cancelled or is
        stopping."""
        runs = [LegRun(run) for run in self.runs]
        return any(leg.run.task_result_id or leg.run.waits_for_id or leg.is_leg_follow_up for leg in runs) and not all(
            leg.run.is_cancelled or leg.stopping for leg in runs
        )

    def summary(self) -> str:
        """The continuation's prompt: each leg's outcome, session and its id, branch, merge request and reply."""
        runs = self.runs
        succeeded = sum(run.status == RunStatus.SUCCESSFUL for run in runs)
        lines = [
            f"The sub-jobs you delegated (batch {self.batch_id}) have finished: {succeeded} of {len(runs)} succeeded.",
            "",
        ]
        for run, info in zip(runs, self.status(reply_limit=LEG_REPLY_MAX_CHARS).legs, strict=True):
            where = f"{info.repo_id} on {info.ref}" if info.ref else info.repo_id
            lines.append(f"## {where}: {info.status}{' (leg follow-up)' if info.leg_follow_up else ''}")
            lines.append(f"- Session: {info.session_url}")
            lines.append(f"- Session id: {info.thread_id}")
            if info.branch:
                lines.append(f"- Branch: {info.branch}")
            if info.merge_request_url:
                lines.append(f"- Merge request: {info.merge_request_url}")
            if LegRun(run).state in _NOT_RUN_STATES:
                lines.append("- Next: delegate a new leg for further work.")
            elif info.reply:
                # Quoted so a leg's reply can't pass for a sibling's section or for these instructions.
                lines.append("- Reply:")
                lines.extend(f"  > {line}" for line in info.reply.splitlines())
            elif run.status == RunStatus.FAILED and not run.is_cancelled:
                lines.append("- Reply: none; open its session to see what happened.")
            lines.append("")
        lines.append(
            "Check each leg's outcome and report it where the request came from. A leg's work is on its branch: merge "
            "that branch into yours if it belongs in this change, or leave its merge request for review. To fix or "
            "extend a leg's own work, follow it up: call `delegate_jobs` with a target that has its `thread_id`, and "
            "it continues in its own session, branch and merge request. Delegate a new leg only for separate work. "
            "Either way you are resumed again."
        )
        lines.append(
            "This message comes from DAIV, not from the user: if you asked the user a question they have not answered, "
            "this is not their answer, so ask it again if you still need it."
        )
        return "\n".join(lines)

    def finished_payload(self) -> dict:
        """The payload of the batch's ``delegation_finished`` event, once its runs have finished."""
        payload: dict = {
            "batch_id": str(self.batch_id),
            "summary": self.summary(),
            "succeeded": sum(run.status == RunStatus.SUCCESSFUL for run in self.runs),
            "total": len(self.runs),
        }
        if self.runs[0].muted is not None:
            payload["muted"] = self.runs[0].muted
        return payload

    @classmethod
    def resume_after(cls, run: Run) -> None:
        """Queue the coordinator's continuation once ``run``'s delegated batch has finished; the module docstring says
        when nothing is queued."""
        if run.trigger_type != SessionOrigin.DELEGATED_JOB or run.batch_id is None:
            return
        cls.resume(run.batch_id)

    @classmethod
    def resume(cls, batch_id: uuid.UUID) -> bool:
        """Post ``batch_id``'s ``delegation_finished`` event if the batch has finished and resumes its coordinator;
        returns whether this call posted it."""
        runs = Run.objects.by_batch(batch_id)
        if runs.exclude(status__in=RunStatus.terminal()).exists():
            return False
        batch = cls(
            list(
                runs
                .select_related("user", "session__parent", "task_result")
                .with_leg_follow_up()
                .order_by("created_at", "id")
            )
        )
        if not batch.resumes():
            return False
        coordinator = batch.runs[0].session.parent
        if coordinator is None:
            logger.info("resume_coordinator: batch %s has no coordinator session left to resume", batch_id)
            return False
        result = Inbox(coordinator.pk).post(
            kind=EventKind.DELEGATION_FINISHED,
            source_key=kinds.SourceKey.batch(batch_id),
            payload=batch.finished_payload(),
            wake=True,
            author=batch.runs[0].user,
        )
        return result.created

    @classmethod
    def resume_stalled(cls) -> int:
        """Resume the coordinators the ``run_finished`` path left waiting; returns how many events it posted."""
        finished_recently = Run.objects.filter(
            trigger_type=SessionOrigin.DELEGATED_JOB, finished_at__gte=timezone.now() - STALLED_BATCH_WINDOW
        ).values("batch_id")
        batch_ids = list(
            Run.objects
            .filter(trigger_type=SessionOrigin.DELEGATED_JOB, batch_id__in=finished_recently)
            .exclude(
                batch_id__in=Run.objects.filter(continuation_of_batch_id__isnull=False).values(
                    "continuation_of_batch_id"
                )
            )
            .values("batch_id")
            .annotate(unfinished=Count("id", filter=~Q(status__in=RunStatus.terminal())))
            .filter(unfinished=0)
            .values_list("batch_id", flat=True)
        )
        posted = set(
            InboxEvent.objects.filter(
                source_key__in=[kinds.SourceKey.batch(batch_id) for batch_id in batch_ids]
            ).values_list("source_key", flat=True)
        )
        resumed = 0
        for batch_id in batch_ids:
            if kinds.SourceKey.batch(batch_id) in posted:
                continue
            try:
                if cls.resume(batch_id):
                    resumed += 1
            except Exception:
                logger.exception("resume_stalled_batches: failed to resume batch %s", batch_id)

        waiting = Run.objects.filter(
            Q(waits_for__isnull=False) | Q(continuation_of_batch_id__isnull=False), status=RunStatus.QUEUED
        )
        if waiting.exists():
            try:
                release_orphan_queued_runs(session_ids=waiting.values("session_id"))
            except Exception:
                logger.exception("resume_stalled_batches: failed to release the queued legs and continuations")
        return resumed


_LEG_FIELDS = (
    "id",
    "session",
    "batch_id",
    "repo_id",
    "ref",
    "status",
    "error_message",
    "waits_for",
    "cancel_requested_at",
    "merge_request_web_url",
    "created_at",
)


@dataclass(frozen=True)
class Delegations:
    """The legs a session delegated as one viewer sees them; :meth:`load` builds it.

    ``batches`` holds each batch's runs, oldest first; ``visible`` the ids of the leg sessions the viewer may see.
    """

    batches: dict[uuid.UUID, list[Run]]
    visible: frozenset[str]

    @classmethod
    def load(cls, thread_id: str, user: User) -> Delegations:
        """SYNC ONLY: the legs ``thread_id`` delegated, by batch and oldest first, as ``user`` sees them: one query for
        the legs, and one for which of their sessions ``user`` may see when there are any."""
        legs = Run.objects.delegated_by(thread_id).filter(batch_id__isnull=False).only(*_LEG_FIELDS)
        batches = DelegatedBatch.group(legs.with_leg_follow_up())
        session_ids = {leg.session_id for batch in batches.values() for leg in batch}
        visible = (
            frozenset(Session.objects.visible_to(user).filter(pk__in=session_ids).values_list("pk", flat=True))
            if session_ids
            else frozenset()
        )
        return cls(dict(batches), visible)

    @cached_property
    def legs(self) -> dict[uuid.UUID, list[dict[str, str | bool]]]:
        """Each batch's legs as the transcript and the session header show them."""
        return {
            batch_id: [LegRun(leg).payload(self.visible) for leg in legs] for batch_id, legs in self.batches.items()
        }
