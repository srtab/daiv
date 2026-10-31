from __future__ import annotations

import logging
from functools import partial
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.tasks.signals import task_finished, task_started

from allauth.socialaccount.models import SocialAccount
from django_tasks_db.backend import TaskResult as DatabaseTaskResult

from core import ui_events
from sessions.delegation import resume_coordinator
from sessions.inbox.complete import on_run_ended
from sessions.inbox.drain import Requester
from sessions.models import EventState, InboxEvent, MergeRequestOwnership, Run, RunStatus, Session
from sessions.queue import dispatch_next_queued, release_runs_waiting_for
from sessions.signal_defs import run_ended, run_finished

logger = logging.getLogger("daiv.sessions")


@receiver(post_save, sender=SocialAccount, dispatch_uid="sessions.claim_runs_for_linked_account")
def claim_runs_for_linked_account(sender: type, instance: SocialAccount, created: bool, **kwargs: Any) -> None:
    """Give the DAIV user who just linked a git platform account what that account started while unlinked, matched by
    the account's id, never by username: its ownerless runs and sessions, those sessions' unowned merge requests, and
    its requests still waiting in an inbox. A run still queued for the account becomes the user's request, keeping its
    id, unless they already have one queued on its branch, which the drain then merges it into. A failure is logged and
    rolled back alone, never breaking the sign-in."""
    if not created:
        return
    match = {"user__isnull": True, "external_provider": instance.provider, "external_uid": instance.uid}
    account = (instance.provider, instance.uid, instance.user_id)
    try:
        with transaction.atomic():
            runs = Run.objects.filter(**match).exclude(status=RunStatus.QUEUED).update(user_id=instance.user_id)
            for queued_id in Run.objects.filter(**match, status=RunStatus.QUEUED).values_list("pk", flat=True):
                runs += _hand_over_queued(queued_id, instance.user_id)
            MergeRequestOwnership.objects.filter(
                session__in=Session.objects.filter(**match), owner__isnull=True
            ).update(owner_id=instance.user_id)
            sessions = Session.objects.filter(**match).update(user_id=instance.user_id)
            InboxEvent.objects.filter(
                author__isnull=True,
                author_provider=instance.provider,
                author_uid=instance.uid,
                state=EventState.PENDING,
            ).update(author_id=instance.user_id)
    except Exception:
        logger.exception("Failed to give the runs of %s account %s to user pk=%s", *account)
        return
    if runs or sessions:
        logger.info("Gave %d run(s) and %d session(s) of %s account %s to user pk=%s", runs, sessions, *account)


def _hand_over_queued(run_id: object, user_id: int) -> int:
    """Make the QUEUED run ``run_id`` a request of ``user_id``, as the inbox records one; 0 when a QUEUED run of theirs
    already holds its place (``run_one_next_run_per_user``)."""
    try:
        with transaction.atomic():
            return Run.objects.filter(pk=run_id).update(**Requester(user_id).run_fields())
    except IntegrityError:
        return 0


@receiver(post_save, sender=Run, dispatch_uid="sessions.publish_nav_runs_changed")
def publish_nav_runs_changed(sender: type, instance: Any, created: bool, **kwargs: Any) -> None:
    """Poke the nav SSE readers whenever a Run's status may have moved.

    The sidebar's "N running" badge is per-viewer, so readers recompute their own
    count from this poke (see ``core.ui_events``). Deferred to commit so the recount
    cannot read a pre-write snapshot.

    On an update the gate is a deliberate superset — a full save carries no
    ``update_fields`` to inspect — because an extra poke costs one COUNT per reader
    while a missed one leaves a stale badge until the connection's next resync. A
    create is exact instead: the status is in hand, and every other run creation
    (``READY``/``QUEUED`` from the webhook, API, MCP, schedule and batch paths) is a
    row no reader's count can include. A run's finish and a sync's non-terminal write skip this
    signal: ``RunManager.finish`` and ``RunManager.update_unfinished`` refresh the badge themselves.
    """
    if created:
        if instance.status == RunStatus.RUNNING:
            transaction.on_commit(ui_events.publisher.runs_changed)
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "status" not in update_fields:
        return
    transaction.on_commit(ui_events.publisher.runs_changed)


def _sync_run_for_task(task_result_id: Any) -> None:
    """Pull latest status/timing/result from the linked DBTaskResult into the Run row.

    Silently no-ops if no Run is linked to the given task_result_id (e.g. tasks that
    don't create a Run, or the brief cross-process race where ``task_started`` fires
    before the Run row is committed on the web side — ``task_finished`` will catch up).
    Errors are swallowed and logged so the worker loop is never crashed by sync failures.
    """
    try:
        run = (
            Run.objects
            .select_related("task_result", "session", "session__scheduled_job", "session__scheduled_job__user", "user")
            .filter(task_result_id=task_result_id)
            .first()
        )
        if run is None:
            return
        run.sync_and_save()
    except Exception:
        logger.exception("Failed to sync run for task_result_id=%s", task_result_id)


@receiver([task_started, task_finished])
def sync_run_on_task_signal(sender: type, task_result: Any, **kwargs: Any) -> None:
    """Sync the linked Run on task state transitions (RUNNING / terminal).

    The django-tasks worker commits DBTaskResult state (via ``claim``/``set_successful``/
    ``set_failed``) before dispatching these signals, so reading back the row here is safe
    without a transaction guard. Only a database result can link to a Run, so results from
    any other backend (the test ``ImmediateBackend``) are skipped.
    """
    if not isinstance(task_result, DatabaseTaskResult):
        return
    _sync_run_for_task(task_result.id)


@receiver(run_finished)
def dispatch_next_in_session(sender: type, run: Any, **kwargs: Any) -> None:
    """Release the next QUEUED run on the finished run's session (:func:`sessions.queue.dispatch_next_queued`).

    ``skip_dispatch=True`` (set when the dispatcher or orphan sweep fails a run it could not enqueue or link)
    suppresses re-entry while still letting the other receivers fire.
    """
    if kwargs.get("skip_dispatch"):
        return
    if session_id := getattr(run, "session_id", None):
        dispatch_next_queued(session_id)


@receiver(run_ended, dispatch_uid="sessions.release_waiting_runs")
def release_waiting_runs(sender: type, run: Any, **kwargs: Any) -> None:
    """Start the QUEUED runs that waited for ``run`` (:func:`sessions.queue.release_runs_waiting_for`).

    On ``run_ended`` rather than ``run_finished`` so a chat turn that delegated releases its waiting legs too.
    """
    release_runs_waiting_for(run)


@receiver(run_ended, dispatch_uid="sessions.settle_inbox_events")
def settle_inbox_events(sender: type, run: Any, **kwargs: Any) -> None:
    """Settle the inbox events ``run`` took and start the session's next run (:mod:`sessions.inbox.complete`).

    On ``run_ended`` so a chat turn's end settles and drains too. ``skip_dispatch`` leaves the drain to the sweep.
    """
    on_run_ended(run, drain=not kwargs.get("skip_dispatch"))


@receiver(run_finished, dispatch_uid="sessions.resume_delegating_session")
def resume_delegating_session(sender: type, run: Any, **kwargs: Any) -> None:
    """Resume the coordinator once its delegated batch has finished (:func:`sessions.delegation.resume_coordinator`).

    Ignores ``skip_dispatch``: a leg the dispatcher failed still finishes its batch.
    """
    resume_coordinator(run)


@receiver(post_delete, sender="agent_sessions.RunArtifact", dispatch_uid="sessions.delete_artifact_file")
def delete_artifact_file(sender: type, instance: Any, **kwargs: Any) -> None:
    """Remove the stored bytes once the row's deletion commits (Django never deletes FileField content itself)."""
    from sessions.artifacts import delete_artifact_files

    if instance.file.name:
        transaction.on_commit(
            partial(delete_artifact_files, instance.file.storage, instance.file.name, str(instance.pk))
        )
