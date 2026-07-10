"""Backfill Session/Run rows from historical Activity and ChatThread tables.

Called by migration 0002. Kept as a standalone module so the logic can be
exercised in isolation. It takes an ``apps`` registry (never imports the models
directly) because the source ``Activity``/``ChatThread`` models no longer exist
in the live app registry — they are dropped by ``activity 0016`` / ``chat 0004``
right after this backfill runs. Tests must therefore drive it through a
historical-state registry (see ``tests/unit_tests/sessions/test_backfill.py``,
which uses ``MigrationExecutor``), not the current one.

Idempotent: rows that already exist are skipped, so re-running after a partial
failure is safe.
"""

from __future__ import annotations

import logging
import uuid

from django.db import models

logger = logging.getLogger("daiv.sessions")

# Discriminator columns whose value is meaningful, never an empty string. A NULL here is real
# corruption (and "" would violate their enum check constraints), so they are left untouched by
# the NULL->"" coalescing below and surface loudly if ever NULL.
_DISCRIMINATOR_FIELDS = frozenset({"origin", "trigger_type", "status"})


def _blank_null_text(obj) -> None:
    """Coalesce NULL -> "" on every non-nullable text column of ``obj``.

    Legacy Activity/ChatThread rows can carry NULL on columns that are NOT NULL on
    Session/Run (those columns pre-date their NOT NULL / ``default=""`` tightening on some
    deployments). A single stray NULL would otherwise abort the whole atomic migration. Uses
    model introspection so every current and future text column is covered without re-listing.
    """
    for field in obj._meta.concrete_fields:
        if field.attname in _DISCRIMINATOR_FIELDS or field.null:
            continue
        if isinstance(field, (models.CharField, models.TextField)) and getattr(obj, field.attname) is None:
            setattr(obj, field.attname, "")


RUN_COPY_FIELDS = [
    # Activity field -> Run field, 1:1 names
    "trigger_type",
    "status",
    "task_result_id",
    "user_id",
    "external_username",
    "title",
    "batch_id",
    "repo_id",
    "ref",
    "prompt",
    "agent_model",
    "agent_thinking_level",
    "notify_on",
    "mention_comment_id",
    "merge_request_iid",
    "merge_request_web_url",
    "sandbox_environment_id",
    "result_summary",
    "error_message",
    "code_changes",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
    "usage_by_model",
    "created_at",
    "started_at",
    "finished_at",
]


def run_backfill(apps, schema_editor=None) -> None:
    Activity = apps.get_model("activity", "Activity")
    ChatThread = apps.get_model("chat", "ChatThread")
    Session = apps.get_model("agent_sessions", "Session")
    Run = apps.get_model("agent_sessions", "Run")

    existing_runs = set(Run.objects.values_list("id", flat=True))
    existing_sessions = set(Session.objects.values_list("thread_id", flat=True))

    # Pass 1: activities, grouped by thread, oldest first so "first wins" fields
    # come from the earliest row and "latest wins" fields overwrite as we walk.
    sessions_to_create: dict[str, Session] = {}
    runs_to_create: list[Run] = []
    for activity in Activity.objects.order_by("created_at", "id").iterator():
        if activity.id in existing_runs:
            continue  # already backfilled — also prevents re-minting sessions for null-thread rows
        thread_id = activity.thread_id or str(uuid.uuid4())  # mint for legacy null-thread rows
        if thread_id not in sessions_to_create and thread_id not in existing_sessions:
            session = Session(
                thread_id=thread_id,
                origin=activity.trigger_type,  # earliest activity wins
                user_id=activity.user_id,
                external_username=activity.external_username,
                repo_id=activity.repo_id,
                ref=activity.ref,
                title=activity.title,
                agent_model=activity.agent_model,
                agent_thinking_level=activity.agent_thinking_level,
                sandbox_environment_id=activity.sandbox_environment_id,
                scheduled_job_id=activity.scheduled_job_id,
                issue_iid=activity.issue_iid,
                merge_request_iid=activity.merge_request_iid,
                created_at=activity.created_at,
                last_active_at=activity.finished_at or activity.created_at,
            )
            _blank_null_text(session)  # legacy rows may carry NULL on NOT NULL text columns
            sessions_to_create[thread_id] = session
        elif thread_id in sessions_to_create:
            session = sessions_to_create[thread_id]
            # Latest-wins fields.
            if activity.title:
                session.title = activity.title
            if activity.issue_iid:
                session.issue_iid = activity.issue_iid
            if activity.merge_request_iid:
                session.merge_request_iid = activity.merge_request_iid
            if activity.sandbox_environment_id:
                session.sandbox_environment_id = activity.sandbox_environment_id
            if activity.scheduled_job_id:
                session.scheduled_job_id = activity.scheduled_job_id
            # First-wins fields backfilled only if still empty.
            if session.user_id is None and activity.user_id is not None:
                session.user_id = activity.user_id
            if not session.external_username and activity.external_username:
                session.external_username = activity.external_username
            session.last_active_at = max(session.last_active_at, activity.finished_at or activity.created_at)

        if activity.id not in existing_runs:
            run = Run(id=activity.id, session_id=thread_id)
            for field in RUN_COPY_FIELDS:
                setattr(run, field, getattr(activity, field))
            _blank_null_text(run)  # legacy rows may carry NULL on NOT NULL text columns
            runs_to_create.append(run)

    # Wrap so a DB error names the failing step instead of surfacing as an opaque
    # DataError with no context (the whole migration is atomic and rolls back).
    try:
        Session.objects.bulk_create(sessions_to_create.values(), batch_size=500)
        Run.objects.bulk_create(runs_to_create, batch_size=500)
    except Exception as err:
        raise RuntimeError(
            f"backfill: bulk_create failed while copying {len(sessions_to_create)} sessions / "
            f"{len(runs_to_create)} runs from Activity ({type(err).__name__}: {err})"
        ) from err

    logger.info(
        "backfill pass 1 (activity): created %d sessions, %d runs", len(sessions_to_create), len(runs_to_create)
    )

    # Pass 2: chat threads. For a session that already exists (activity-origin),
    # chat wins for title/model pins and last_active_at is max-of-both; ``user``
    # is first-wins (kept from the earliest activity, only filled if still empty).
    # Otherwise a fresh chat-origin session is created.
    chat_created = 0
    chat_merged = 0
    for thread in ChatThread.objects.iterator():
        session, created = Session.objects.get_or_create(
            thread_id=thread.thread_id,
            defaults={
                "origin": "chat",
                "user_id": thread.user_id,
                # NOT NULL on Session; legacy chat rows may carry NULL on these text columns.
                "repo_id": thread.repo_id or "",
                "ref": thread.ref or "",
                "title": thread.title or "",
                "agent_model": thread.agent_model or "",
                "agent_thinking_level": thread.agent_thinking_level or "",
                "sandbox_environment_id": thread.sandbox_environment_id,
                "created_at": thread.created_at,
                "last_active_at": thread.last_active_at,
            },
        )
        if created:
            chat_created += 1
            continue
        chat_merged += 1
        update_fields = []
        if thread.title:
            session.title = thread.title
            update_fields.append("title")
        if session.user_id is None and thread.user_id is not None:
            session.user_id = thread.user_id
            update_fields.append("user_id")
        if thread.last_active_at > session.last_active_at:
            session.last_active_at = thread.last_active_at
            update_fields.append("last_active_at")
        if thread.agent_model and thread.agent_model != session.agent_model:
            session.agent_model = thread.agent_model
            update_fields.append("agent_model")
        if thread.agent_thinking_level and thread.agent_thinking_level != session.agent_thinking_level:
            session.agent_thinking_level = thread.agent_thinking_level
            update_fields.append("agent_thinking_level")
        if update_fields:
            session.save(update_fields=update_fields)

    logger.info("backfill pass 2 (chat): created %d sessions, merged %d into existing", chat_created, chat_merged)
