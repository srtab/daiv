"""Tests for the Activity/ChatThread -> Session/Run data backfill (migration 0002).

The source ``Activity``/``ChatThread`` models are dropped from the live registry
by ``activity 0016`` / ``chat 0004`` right after the backfill runs, so the logic
can only be exercised through a historical-state registry. We migrate
``agent_sessions`` down to ``0001_initial`` (which reverses the drops and
re-creates the source tables), build the pre-0002 project state, seed the source
tables via the historical models, then call ``run_backfill`` directly — the same
callable migration 0002 invokes.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

import pytest
from sessions.backfill import run_backfill

from accounts.models import User

PRE_BACKFILL = ("agent_sessions", "0002_backfill_from_activity_and_chat")


@pytest.fixture(autouse=True)
def _restore_migrations_to_head():
    """Re-apply all migrations to head after each test.

    Migrating ``agent_sessions`` down to 0001 reverses ``activity 0016`` /
    ``chat 0004`` (re-creating the source tables) and the backfill. The test DB is
    a single in-memory SQLite shared across the session, so without restoring head
    every later Session/Run test would fail with "no such table".
    """
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    executor.loader.build_graph()


@pytest.fixture
def h():
    """Migrate down to the pre-backfill state and expose the historical models.

    Attribute access (``h.activity``) rather than local ``Model = ...`` bindings so
    the model classes don't trip the uppercase-local lint rule.
    """
    executor = MigrationExecutor(connection)
    executor.migrate([("agent_sessions", "0001_initial")])
    executor.loader.build_graph()
    apps = executor.loader.project_state(PRE_BACKFILL, at_end=False).apps
    return SimpleNamespace(
        apps=apps,
        activity=apps.get_model("activity", "Activity"),
        chat=apps.get_model("chat", "ChatThread"),
        session=apps.get_model("agent_sessions", "Session"),
        run=apps.get_model("agent_sessions", "Run"),
    )


def _mk_user(username):
    # Use the LIVE User model: only ``agent_sessions`` was migrated down, so the
    # accounts_user table is still at head schema (columns the historical User
    # model wouldn't know about, e.g. ``role NOT NULL``). We only need the pk.
    return User.objects.create_user(username=username, email=f"{username}@t.co", password="x")  # noqa: S106


def _mk_activity(model, *, created_at, **kwargs):
    kwargs.setdefault("trigger_type", "api_job")
    kwargs.setdefault("status", "SUCCESSFUL")
    kwargs.setdefault("repo_id", "group/project")
    activity = model.objects.create(**kwargs)
    # created_at is auto_now_add; override via queryset to control merge ordering.
    model.objects.filter(pk=activity.pk).update(created_at=created_at)
    activity.refresh_from_db()
    return activity


@pytest.mark.django_db(transaction=True)
def test_backfill_activities_merge_preserve_uuid_and_mint_null_threads(h):
    user = _mk_user("owner")

    # Two activities sharing one thread: oldest first (issue_webhook, no user),
    # newest last (api_job, has user, newer title).
    older = _mk_activity(
        h.activity,
        thread_id="T-merge",
        trigger_type="issue_webhook",
        user_id=None,
        external_username="ext-user",
        title="first title",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = _mk_activity(
        h.activity,
        thread_id="T-merge",
        trigger_type="api_job",
        user_id=user.pk,
        title="second title",
        input_tokens=100,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    # An activity with a null thread_id must still get its own minted session.
    orphan = _mk_activity(
        h.activity, thread_id=None, trigger_type="mcp_job", created_at=datetime(2026, 1, 3, tzinfo=UTC)
    )

    run_backfill(h.apps)

    # One session for the shared thread + one minted for the null-thread orphan.
    assert h.session.objects.count() == 2
    merged = h.session.objects.get(thread_id="T-merge")
    assert merged.origin == "issue_webhook"  # first (earliest) activity wins
    assert merged.title == "second title"  # latest wins
    assert merged.user_id == user.pk  # first-wins backfilled from the row that had one
    assert merged.external_username == "ext-user"

    # UUIDs are preserved so external job IDs keep resolving.
    assert h.run.objects.filter(pk=older.pk, session_id="T-merge", trigger_type="issue_webhook").exists()
    assert h.run.objects.filter(pk=newer.pk, session_id="T-merge", input_tokens=100).exists()

    orphan_run = h.run.objects.get(pk=orphan.pk)
    assert orphan_run.session_id  # minted, non-empty
    assert orphan_run.session_id != "T-merge"
    assert h.session.objects.get(thread_id=orphan_run.session_id).origin == "mcp_job"


@pytest.mark.django_db(transaction=True)
def test_backfill_chat_merges_into_activity_session_user_is_first_wins(h):
    activity_user = _mk_user("activity-owner")
    chat_user = _mk_user("chat-owner")

    _mk_activity(
        h.activity,
        thread_id="T-shared",
        trigger_type="api_job",
        user_id=activity_user.pk,
        title="activity title",
        agent_model="model-a",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    # Chat thread continuing the same thread_id, with a *different* user, newer title/model.
    chat = h.chat.objects.create(
        thread_id="T-shared", user_id=chat_user.pk, repo_id="group/project", title="chat title", agent_model="model-b"
    )
    h.chat.objects.filter(pk=chat.pk).update(last_active_at=datetime(2026, 2, 9, tzinfo=UTC))

    # A chat-only thread with no activity.
    h.chat.objects.create(thread_id="T-chat-only", user_id=chat_user.pk, repo_id="group/project", title="solo")

    run_backfill(h.apps)

    shared = h.session.objects.get(thread_id="T-shared")
    assert shared.origin == "api_job"  # origin stays from the activity
    assert shared.title == "chat title"  # chat wins for title
    assert shared.agent_model == "model-b"  # chat wins for model pins
    # The load-bearing assertion: user is FIRST-wins, kept from the activity — NOT
    # overwritten by the chat thread's user.
    assert shared.user_id == activity_user.pk
    assert shared.last_active_at == datetime(2026, 2, 9, tzinfo=UTC)  # max-wins

    solo = h.session.objects.get(thread_id="T-chat-only")
    assert solo.origin == "chat"
    assert h.run.objects.filter(session_id="T-chat-only").count() == 0


@pytest.mark.django_db(transaction=True)
def test_backfill_is_idempotent(h):
    _mk_activity(h.activity, thread_id="T1", created_at=datetime(2026, 3, 1, tzinfo=UTC))
    h.chat.objects.create(thread_id="T2", user_id=_mk_user("u").pk, repo_id="group/project")

    run_backfill(h.apps)
    sessions_after_first = h.session.objects.count()
    runs_after_first = h.run.objects.count()

    # Second run must not duplicate rows.
    run_backfill(h.apps)
    assert h.session.objects.count() == sessions_after_first
    assert h.run.objects.count() == runs_after_first
